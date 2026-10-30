"""AD5: comparación independiente por ciclo, activo y referencia oficial."""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from sqlmodel import Session, select

from app.database import engine
from app.models import Cycle, LedgerEntry, SwapRecord, WalletBalanceSnapshot, WalletStatementEntry
from app.services.wallet_service import WalletService
from app.services.wallet_client import WalletBalanceResponse, WalletStatementPage
from app.services.wallet_reconciliation import (
    fetch_incremental_statement_entries, reconcile_wallet, store_statement_entries,
)


class Wallet:
    def __init__(self, balance, pages):
        self.balance = balance
        self.pages = list(pages)
        self.cursors = []

    async def get_balance(self):
        return self.balance

    async def get_statement(self, *, cycle_id=None, cursor=None):
        self.cursors.append(cursor)
        return self.pages.pop(0)


def _balance(now, *, cr="500", kwh="0", cycle_id=None, close_cr=None):
    return WalletBalanceResponse(
        city_id="KLD", cr_available=Decimal(cr), cr_held=Decimal("0"),
        kwh_available=Decimal(kwh), kwh_held=Decimal("0"),
        credit_line=Decimal("0"), timestamp=now.isoformat(),
        last_settled_cycle=cycle_id,
        balance_at_last_close_cr=Decimal(close_cr) if close_cr is not None else None,
    )


def _cycle(session, now, *, budget="500", energy="0"):
    cycle_id = f"e2070-{uuid4()}"
    session.add(Cycle(
        cycle_id=cycle_id, status_idpk=str(uuid4()),
        opening_budget_balance=Decimal("500"), budget_balance=Decimal(budget),
        opening_energy_balance=Decimal("0"), energy_balance=Decimal(energy),
        status_payload={"energy": {"sellable": 0}}, created_at=now,
    ))
    session.flush()
    return cycle_id


def _event(session, cycle_id, event_at, *, cr="500", kwh="0"):
    session.add(WalletBalanceSnapshot(
        cycle_id=cycle_id, source="EVENT", source_swap_id="swp-before",
        wallet_as_of=event_at, recorded_at=event_at,
        cr_available=Decimal(cr), kwh_available=Decimal(kwh),
        cr_held=Decimal("0"), kwh_held=Decimal("0"),
    ))
    session.commit()


def _entry(cycle_id, at, asset, amount, reference="swp-77"):
    return {"entryId": str(uuid4()), "cycleId": cycle_id, "asset": asset,
            "kind": "swap", "amount": float(amount), "counterparty": "HGW",
            "reference": reference, "createdAt": at.isoformat()}


def test_match_requires_wallet_time_and_consistent_local_cycle():
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        cycle_id = _cycle(session, now)
        _event(session, cycle_id, now - timedelta(minutes=1))
        report = asyncio.run(reconcile_wallet(session, client=Wallet(
            _balance(now, cycle_id=cycle_id, close_cr="500"),
            [WalletStatementPage(entries=[], next_cursor=None)],
        ), now=now))
    assert report.status == "MATCH"
    assert report.explanation["local_evidence"]["ledger_materialization"]["consistent"]


def test_equal_event_and_rest_balances_do_not_hide_local_difference():
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        cycle_id = _cycle(session, now, budget="120")
        # Opening 500 y materializado 120 sin asiento: defecto local confirmado.
        _event(session, cycle_id, now - timedelta(minutes=1))
        report = asyncio.run(reconcile_wallet(session, client=Wallet(
            _balance(now), [WalletStatementPage(entries=[], next_cursor=None)]
        ), cycle_id=cycle_id, now=now))
    assert report.cr_difference == 0
    assert report.status == "DIVERGENT"
    assert report.explanation["local_evidence"]["single_cycle_cr"]["difference"] == "380.00"


def test_swap_statement_correlates_entry_ids_with_ledger_by_swap_id():
    now = datetime.now(timezone.utc)
    cycle_time = now - timedelta(minutes=2)
    with Session(engine) as session:
        cycle_id = _cycle(session, now, budget="450", energy="2")
        session.add(LedgerEntry(
            cycle_id=cycle_id, sequence_no=1, operation_type="SWAP_SETTLED",
            idpk="swp-77", budget_delta=Decimal("-50"), energy_delta=Decimal("2"),
            budget_after=Decimal("450"), energy_after=Decimal("2"),
            details={"swapId": "swp-77"}, applied_at=cycle_time,
        ))
        _event(session, cycle_id, cycle_time, cr="500")
        entries = [_entry(cycle_id, now - timedelta(minutes=1), "CR", -50),
                   _entry(cycle_id, now - timedelta(minutes=1), "KWH", 2)]
        report = asyncio.run(reconcile_wallet(session, client=Wallet(
            _balance(now, cr="450", kwh="2"),
            [WalletStatementPage(entries=entries, next_cursor=None)],
        ), cycle_id=cycle_id, now=now))
    assert report.status == "EXPLAINED"
    operations = report.explanation["local_evidence"]["operations"]
    assert len(operations) == 2
    assert {row["status"] for row in operations} == {"MATCH"}
    assert {row["reference"] for row in operations} == {"swp-77"}
    assert {row["wallet_entry_ids"][0] for row in operations} == {item["entryId"] for item in entries}


def test_same_reference_with_wrong_local_amount_is_explained_as_mismatch():
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        cycle_id = _cycle(session, now, budget="460")
        session.add(LedgerEntry(
            cycle_id=cycle_id, sequence_no=1, operation_type="SWAP_SETTLED",
            idpk="swp-77", budget_delta=Decimal("-40"), energy_delta=Decimal("0"),
            budget_after=Decimal("460"), energy_after=Decimal("0"),
            details={"swapId": "swp-77"}, applied_at=now - timedelta(minutes=1),
        ))
        _event(session, cycle_id, now - timedelta(minutes=2))
        wallet_entry = _entry(cycle_id, now - timedelta(minutes=1), "CR", -50)
        report = asyncio.run(reconcile_wallet(session, client=Wallet(
            _balance(now, cr="450"),
            [WalletStatementPage(entries=[wallet_entry], next_cursor=None)],
        ), cycle_id=cycle_id, now=now))
    assert report.status == "DIVERGENT"
    operation = report.explanation["local_evidence"]["operations"][0]
    assert operation["status"] == "MISMATCH"
    assert Decimal(operation["difference"]) == Decimal("-10")
    assert operation["wallet_entry_ids"] == [wallet_entry["entryId"]]


def test_funding_penalty_transport_and_imbalance_use_distinct_local_effects():
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        cycle_id = _cycle(session, now, budget="610")
        effects = [
            ("TRANSFER_IN", "funding-idpk", Decimal("100")),
            ("SWAP_TRANSPORT", "swp-77", Decimal("-10")),
            ("IMBALANCE_SETTLEMENT", "settlement-id", Decimal("20")),
        ]
        running = Decimal("500")
        for sequence, (operation, idpk, delta) in enumerate(effects, 1):
            running += delta
            session.add(LedgerEntry(
                cycle_id=cycle_id, sequence_no=sequence, operation_type=operation,
                idpk=idpk, budget_delta=delta, energy_delta=Decimal("0"),
                budget_after=running, energy_after=Decimal("0"),
                details={}, applied_at=now - timedelta(minutes=1),
            ))
        _event(session, cycle_id, now - timedelta(minutes=2))
        entries = []
        for kind, amount, reference in (
            ("funding", 120, "transfer-1"), ("penalty", -20, "penalty-1"),
            ("transport", -10, "swp-77"), ("buyback", 20, "settlement-id"),
        ):
            row = _entry(cycle_id, now - timedelta(minutes=1), "CR", amount, reference)
            row["kind"] = kind
            entries.append(row)
        report = asyncio.run(reconcile_wallet(session, client=Wallet(
            _balance(now, cr="610"),
            [WalletStatementPage(entries=entries, next_cursor=None)],
        ), cycle_id=cycle_id, now=now))
    assert report.status == "EXPLAINED"
    operations = report.explanation["local_evidence"]["operations"]
    assert {row["operation"] for row in operations} == {
        "TRANSFER_IN", "SWAP_TRANSPORT", "IMBALANCE_SETTLEMENT",
    }
    assert {row["status"] for row in operations} == {"MATCH"}
    funding = next(row for row in operations if row["operation"] == "TRANSFER_IN")
    assert Decimal(funding["wallet_amount"]) == Decimal("100")
    assert len(funding["wallet_entry_ids"]) == 2


def test_statement_created_after_balance_asof_is_not_counted():
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        cycle_id = _cycle(session, now)
        _event(session, cycle_id, now - timedelta(minutes=1))
        late = _entry(cycle_id, now + timedelta(seconds=1), "CR", 100)
        report = asyncio.run(reconcile_wallet(session, client=Wallet(
            _balance(now), [WalletStatementPage(entries=[late], next_cursor=None)]
        ), now=now))
    assert report.status == "MATCH"
    assert report.cr_explained == 0


def test_future_event_is_not_used_as_baseline():
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        cycle_id = f"e2070-future-{uuid4()}"
        _event(session, cycle_id, now + timedelta(minutes=1))
        report = asyncio.run(reconcile_wallet(session, client=Wallet(
            _balance(now), [WalletStatementPage(entries=[], next_cursor=None)]
        ), now=now))
    assert report.status == "NO_LOCAL_BASELINE"
    assert report.cr_available_local is None


def test_settled_event_without_cycle_keeps_snapshot_but_no_ledger_or_match():
    """El modelo permite el registro huérfano; el evento no crea el ciclo."""
    now = datetime.now(timezone.utc)
    cycle_id = f"e2070-orphan-{uuid4()}"
    swap_id = f"swap-{uuid4()}"
    with Session(engine) as session:
        session.add(SwapRecord(
            idpk=str(uuid4()), swap_id=swap_id, cycle_id=cycle_id,
            role="PROPOSER", counterpart_city_id="HGW", give_asset="KWH",
            give_amount=Decimal("2"), take_asset="CR", take_amount=Decimal("10"),
            status="PROPOSED", idempotency_key=str(uuid4()), created_at=now,
        ))
        session.commit()
        WalletService().process_swap_event(session, event_type="swap-settled", data={
            "swapId": swap_id, "idpk": str(uuid4()), "settledAt": now.isoformat(),
            "balancesAfter": {
                "CR": {"available": 500, "held": 0},
                "KWH": {"available": 0, "held": 0},
            },
        })
        session.commit()
        assert session.get(Cycle, cycle_id) is None
        assert session.exec(select(LedgerEntry).where(LedgerEntry.cycle_id == cycle_id)).all() == []
        assert session.exec(select(WalletBalanceSnapshot).where(
            WalletBalanceSnapshot.cycle_id == cycle_id,
            WalletBalanceSnapshot.source == "EVENT",
        )).first() is not None
        report = asyncio.run(reconcile_wallet(session, client=Wallet(
            _balance(now + timedelta(seconds=1), cr="500", cycle_id=cycle_id),
            [WalletStatementPage(entries=[], next_cursor=None)],
        ), now=now + timedelta(seconds=1)))
    assert report.status == "NO_LOCAL_BASELINE"
    assert report.explanation["local_evidence"]["cycle_present"] is False


def test_local_materialization_gap_without_event_is_reported_without_inventing_baseline():
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        cycle_id = _cycle(session, now, budget="120")
        session.commit()
        report = asyncio.run(reconcile_wallet(session, client=Wallet(
            _balance(now, cr="500"),
            [WalletStatementPage(entries=[], next_cursor=None)],
        ), cycle_id=cycle_id, now=now))
    assert report.status == "NO_LOCAL_BASELINE"
    assert report.explanation["local_difference_without_global_baseline"] is True
    assert report.explanation["local_evidence"]["ledger_materialization"] == {
        "budget_rebuilt": "500.00", "budget_cycle": "120.00",
        "energy_rebuilt": "0.00", "energy_cycle": "0.00", "consistent": False,
    }


def test_incremental_scan_stops_after_overlap_and_full_scan_recovers_late_entry():
    now = datetime.now(timezone.utc)
    cycle_id = f"e2070-pages-{uuid4()}"
    recent = [_entry(cycle_id, now, "CR", 1, reference=f"ref-{index}") for index in range(3)]
    late = _entry(cycle_id, now - timedelta(hours=1), "CR", 1, reference="late")
    with Session(engine) as session:
        store_statement_entries(session, recent, now=now)
        session.commit()
    pages = lambda: [WalletStatementPage(entries=[recent[0]], next_cursor="p2"),
                     WalletStatementPage(entries=[recent[1]], next_cursor="p3"),
                     WalletStatementPage(entries=[recent[2], late], next_cursor=None)]
    with Session(engine) as session:
        partial = Wallet(_balance(now), pages())
        entries, complete, count = asyncio.run(fetch_incremental_statement_entries(
            partial, session, full_scan=False,
        ))
        assert not complete and count == 2 and len(entries) == 2
    with Session(engine) as session:
        exhaustive = Wallet(_balance(now), pages())
        entries, complete, count = asyncio.run(fetch_incremental_statement_entries(
            exhaustive, session, full_scan=True,
        ))
        assert complete and count == 3
        assert late["entryId"] in {entry["entryId"] for entry in entries}
        assert store_statement_entries(session, entries, now=now) == 1
        session.commit()
    with Session(engine) as session:
        assert session.exec(select(WalletStatementEntry).where(
            WalletStatementEntry.entry_id == late["entryId"]
        )).one().reference == "late"


def test_historical_recheck_schedule_survives_new_sessions():
    now = datetime.now(timezone.utc)
    flags = []
    for index in range(11):
        with Session(engine) as session:
            report = asyncio.run(reconcile_wallet(session, client=Wallet(
                _balance(now + timedelta(seconds=index)),
                [WalletStatementPage(entries=[], next_cursor=None)],
            ), now=now + timedelta(seconds=index)))
            flags.append(report.explanation["statement_full_scan"])
    assert flags == [True] + [False] * 9 + [True]
