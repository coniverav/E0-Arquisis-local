"""E2-051: swap, transporte y settlement actualizan el ledger E1."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from sqlmodel import Session, select

from app.database import engine
from app.models import (
    Cycle,
    ImbalanceSettlement,
    LedgerEntry,
    SwapRecord,
    TradeRecord,
)
from app.services.cycle_accounting import (
    apply_imbalance_settlement,
    apply_swap_settled,
    apply_trade_transport,
    finalize_closed_markets,
)
from app.services.ledger import validate_cycle_consistency


def _make_cycle(session, *, cycle_id, budget="0", energy="0", **kwargs):
    now = datetime.now(timezone.utc)
    cycle = Cycle(
        cycle_id=cycle_id,
        status_idpk=str(uuid4()),
        status_payload={"market": {}},
        opening_budget_balance=Decimal(budget),
        opening_energy_balance=Decimal(energy),
        budget_balance=Decimal(budget),
        energy_balance=Decimal(energy),
        last_sequence=0,
        created_at=now,
        valid_until=now + timedelta(minutes=10),
        **kwargs,
    )
    session.add(cycle)
    session.commit()
    return cycle


def test_swap_transport_and_settlement_apply_once_and_close_kwh():
    cycle_id = f"e2051-{uuid4()}"
    swap_id = f"swp-{uuid4().hex}"
    settlement_id = f"set-{uuid4()}"
    now = datetime.now(timezone.utc)

    with Session(engine) as session:
        _make_cycle(session, cycle_id=cycle_id, budget="0", energy="-1000")
        record = SwapRecord(
            idpk=str(uuid4()),
            swap_id=swap_id,
            cycle_id=cycle_id,
            role="BUYER",
            counterpart_city_id="MET",
            give_asset="CR",
            give_amount=Decimal("500"),
            take_asset="KWH",
            take_amount=Decimal("1000"),
            status="SETTLED",
            idempotency_key=str(uuid4()),
            created_at=now,
        )
        session.add(record)
        session.flush()

        entry, applied = apply_swap_settled(session, record=record)
        assert applied is True
        assert entry.operation_type == "SWAP_SETTLED"
        assert entry.budget_delta == Decimal("-500")
        assert entry.energy_delta == Decimal("1000")

        trade = TradeRecord(
            idpk=str(uuid4()),
            msg_id=str(uuid4()),
            cycle_id=cycle_id,
            swap_id=swap_id,
            seller="MET",
            buyer="KLD",
            role="BUYER",
            counterpart_city_id="MET",
            quantity=1000,
            price_per_energy=Decimal("0.5"),
            energy_total=Decimal("500"),
            transport_per_energy=Decimal("0.1"),
            transport_total=Decimal("100"),
            route_enabled=True,
            recorded_at=now,
        )
        session.add(trade)
        session.flush()
        trade_entry, _ = apply_trade_transport(session, record=trade)
        assert trade_entry.operation_type == "SWAP_TRANSPORT"
        assert trade_entry.budget_delta == Decimal("-100")

        settlement = ImbalanceSettlement(
            idpk=str(uuid4()),
            msg_id=str(uuid4()),
            cycle_id=cycle_id,
            settlement_id=settlement_id,
            kwh_held=1000,
            deficit=1000,
            coverage=0,
            price_per_energy=Decimal("2"),
            total=Decimal("0"),
            balance_after=Decimal("-600"),
            recorded_at=now,
        )
        session.add(settlement)
        session.flush()
        apply_imbalance_settlement(session, settlement=settlement)

        cycle = session.get(Cycle, cycle_id)
        assert cycle.budget_balance == Decimal("-600.00")
        assert cycle.energy_balance == 0
        entries = session.exec(
            select(LedgerEntry)
            .where(LedgerEntry.cycle_id == cycle_id)
            .order_by(LedgerEntry.sequence_no)
        ).all()
        assert [entry.operation_type for entry in entries] == [
            "SWAP_SETTLED", "SWAP_TRANSPORT", "IMBALANCE_SETTLEMENT",
        ]
        assert entries[-1].details["kwhHeld"] == 1000
        assert entries[-1].details["coverage"] == 0

        # Replays de los tres hechos: ni un asiento adicional.
        assert apply_swap_settled(session, record=record)[1] is False
        assert apply_trade_transport(session, record=trade)[1] is False
        apply_imbalance_settlement(session, settlement=settlement)
        session.commit()
        assert len(session.exec(
            select(LedgerEntry).where(LedgerEntry.cycle_id == cycle_id)
        ).all()) == 3
        validate_cycle_consistency(session, cycle_id)


def test_closed_market_without_settlement_burns_local_kwh():
    cycle_id = f"e2051-{uuid4()}"
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        _make_cycle(
            session,
            cycle_id=cycle_id,
            budget="0",
            energy="-1000",
            market_closes_at=now - timedelta(seconds=1),
        )

        result = finalize_closed_markets(session, now=now)
        session.commit()

        assert result == {"cycles_examined": 1, "burns_applied": 1}
        cycle = session.get(Cycle, cycle_id)
        assert cycle.energy_balance == 0
        entries = session.exec(
            select(LedgerEntry).where(LedgerEntry.cycle_id == cycle_id)
        ).all()
        assert len(entries) == 1
        assert entries[0].operation_type == "IMBALANCE_BURN_FALLBACK"
        assert entries[0].details["reason"] == "SETTLEMENT_MISSING"

        # Idempotente: el segundo tick no vuelve a quemar.
        assert finalize_closed_markets(session, now=now)["burns_applied"] == 0
        session.commit()
        validate_cycle_consistency(session, cycle_id)
