"""E2-044: conciliación con `GET /balance` y `/statement`.

No se declara al snapshot local como autoridad: el reporte siempre queda con
`authority="wallet"` y explica la diferencia con asientos y hechos locales.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest
from sqlmodel import Session, select

from app.database import engine, run_migrations
from app.models import (
    ImbalanceSettlement,
    TradeRecord,
    WalletBalanceSnapshot,
    WalletReconciliation,
    WalletStatementEntry,
)
from app.services.wallet_client import (
    WalletBalanceResponse,
    WalletClient,
    WalletError,
    WalletRateLimitError,
    WalletStatementPage,
)
from app.services.wallet_reconciliation import (
    fetch_statement_entries,
    reconcile_wallet,
    reconcile_wallet_tick,
)
from app.services import wallet_reconciliation


def setup_module():
    run_migrations()


NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
BASELINE_AT = NOW - timedelta(minutes=5)


def _balance(*, cr="500", kwh="50") -> WalletBalanceResponse:
    return WalletBalanceResponse(
        city_id="KLD",
        kwh_available=Decimal(kwh),
        kwh_held=Decimal("0"),
        cr_available=Decimal(cr),
        cr_held=Decimal("0"),
        credit_line=Decimal("100000"),
        timestamp=NOW.isoformat().replace("+00:00", "Z"),
        last_settled_cycle=None,
        balance_at_last_close_cr=None,
    )


def _entry(asset, kind, amount, *, reference="ref-1", created_at=None, cycle="cycle-1"):
    return {
        "entryId": str(uuid4()),
        "cycleId": cycle,
        "asset": asset,
        "kind": kind,
        "amount": float(amount),
        "counterparty": "HGW",
        "reference": reference,
        "createdAt": (created_at or NOW - timedelta(minutes=1))
        .isoformat()
        .replace("+00:00", "Z"),
    }


class FakeWallet:
    def __init__(self, balance, pages):
        self.balance = balance
        self.pages = list(pages)
        self.calls = []

    async def get_balance(self):
        return self.balance

    async def get_statement(self, cycle_id=None, cursor=None):
        self.calls.append(cursor)
        return self.pages.pop(0)


def _seed_baseline(*, cr="1000", kwh="100"):
    with Session(engine) as session:
        snapshot = WalletBalanceSnapshot(
            cycle_id="cycle-1",
            source="EVENT",
            kwh_available=Decimal(kwh),
            kwh_held=Decimal("0"),
            cr_available=Decimal(cr),
            cr_held=Decimal("0"),
            credit_line=None,
            recorded_at=BASELINE_AT,
        )
        session.add(snapshot)
        session.commit()
        session.refresh(snapshot)
        return snapshot.id


def _run_reconcile(client, *, cycle_id=None):
    with Session(engine) as session:
        report = asyncio.run(
            reconcile_wallet(session, client=client, cycle_id=cycle_id, now=NOW)
        )
        session.refresh(report)
        return report


def test_statement_client_paginates_and_returns_contract_shape():
    client = WalletClient(base_url="https://wallet.test", api_key="key")
    first = httpx.Response(200, json={
        "entries": [_entry("CR", "swap", -10)],
        "nextCursor": "cursor-2",
    })
    second = httpx.Response(200, json={
        "entries": [_entry("KWH", "swap", -1)],
        "nextCursor": None,
    })

    with patch("app.services.wallet_client.ObservedWalletAsyncClient") as observed:
        observed.return_value.__aenter__.return_value.get = AsyncMock(
            side_effect=[first, second]
        )
        page1 = asyncio.run(client.get_statement())
        page2 = asyncio.run(
            client.get_statement(cursor=page1.next_cursor)
        )

    assert len(page1.entries) == 1
    assert page1.next_cursor == "cursor-2"
    assert len(page2.entries) == 1
    assert page2.next_cursor is None


def test_reconcile_explains_difference_with_statement_entries():
    _seed_baseline()
    client = FakeWallet(
        _balance(),
        [WalletStatementPage(entries=[
            _entry("CR", "swap", -500),
            _entry("KWH", "swap", -50),
        ], next_cursor=None)],
    )

    report = _run_reconcile(client, cycle_id="cycle-1")

    assert report.authority == "wallet"
    assert report.status == "EXPLAINED"
    assert report.cr_difference == Decimal("-500")
    assert report.cr_explained == Decimal("-500")
    assert report.cr_unexplained == Decimal("0")
    assert report.kwh_difference == Decimal("-50")
    assert report.kwh_explained == Decimal("-50")
    assert report.kwh_unexplained == Decimal("0")
    assert report.cycle_id == "cycle-1"


def test_reconcile_marks_divergence_when_statement_does_not_explain():
    _seed_baseline()
    client = FakeWallet(
        _balance(),
        [WalletStatementPage(entries=[
            _entry("CR", "transport", -100),
            _entry("KWH", "swap", -50),
        ], next_cursor=None)],
    )

    report = _run_reconcile(client)

    assert report.status == "DIVERGENT"
    assert report.cr_difference == Decimal("-500")
    assert report.cr_explained == Decimal("-100")
    assert report.cr_unexplained == Decimal("-400")
    assert report.kwh_unexplained == Decimal("0")


def test_reconcile_without_event_baseline_never_claims_local_authority():
    client = FakeWallet(
        _balance(),
        [WalletStatementPage(entries=[_entry("CR", "funding", 500)], next_cursor=None)],
    )

    report = _run_reconcile(client)

    assert report.status == "NO_LOCAL_BASELINE"
    assert report.authority == "wallet"
    assert report.cr_available_local is None
    assert report.cr_unexplained is None
    assert report.cr_available_wallet == Decimal("500")


def test_statement_entries_are_deduplicated_across_runs():
    _seed_baseline()
    entry = _entry("CR", "swap", -500)
    pages = [WalletStatementPage(entries=[entry], next_cursor=None)]

    first = _run_reconcile(FakeWallet(_balance(), list(pages)))
    second = _run_reconcile(FakeWallet(_balance(), list(pages)))

    assert first.explanation["statement_entries_new"] == 1
    assert second.explanation["statement_entries_new"] == 0
    with Session(engine) as session:
        assert len(session.exec(select(WalletStatementEntry)).all()) == 1
        assert len(session.exec(select(WalletReconciliation)).all()) == 2


def test_reconcile_explanation_counts_local_wallet_facts():
    _seed_baseline()
    with Session(engine) as session:
        session.add(TradeRecord(
            idpk=str(uuid4()), msg_id=str(uuid4()), cycle_id="cycle-1",
            swap_id="swp-1", offer_id="offer-1", seller="KLD", buyer="HGW",
            role="SELLER", counterpart_city_id="HGW", quantity=10,
            price_per_energy=Decimal("10"), energy_total=Decimal("100"),
            transport_per_energy=Decimal("1"), transport_total=Decimal("10"),
            route_enabled=True, recorded_at=NOW - timedelta(minutes=1),
        ))
        session.add(ImbalanceSettlement(
            idpk=str(uuid4()), msg_id=str(uuid4()), cycle_id="cycle-1",
            settlement_id=str(uuid4()), kwh_held=0, deficit=0, coverage=0,
            price_per_energy=Decimal("10"), total=Decimal("0"),
            balance_after=Decimal("100"), recorded_at=NOW - timedelta(minutes=1),
        ))
        session.commit()

    client = FakeWallet(
        _balance(),
        [WalletStatementPage(entries=[
            _entry("CR", "swap", -500),
            _entry("KWH", "swap", -50),
        ], next_cursor=None)],
    )

    report = _run_reconcile(client)

    facts = report.explanation["local_facts"]
    assert facts["trade_records"] == 1
    assert facts["imbalance_settlements"] == 1


def test_reconcile_tick_throttles_between_runs(monkeypatch):
    _seed_baseline()
    monkeypatch.setattr(wallet_reconciliation, "_last_tick_monotonic", None)
    clock = [100.0]
    monkeypatch.setattr(wallet_reconciliation, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    client = FakeWallet(
        _balance(),
        [
            WalletStatementPage(entries=[], next_cursor=None),
            WalletStatementPage(entries=[], next_cursor=None),
        ],
    )

    first = asyncio.run(
        reconcile_wallet_tick(interval_seconds=3600, client=client, now=NOW)
    )
    second = asyncio.run(
        reconcile_wallet_tick(interval_seconds=3600, client=client, now=NOW)
    )

    assert first is not None
    assert second is None
    assert len(client.calls) == 1

    clock[0] = 3701.0
    third = asyncio.run(
        reconcile_wallet_tick(interval_seconds=3600, client=client, now=NOW)
    )
    assert third is not None
    assert len(client.calls) == 2


def test_statement_is_fetched_from_first_page_again_on_every_reconciliation():
    _seed_baseline(cr="500", kwh="50")
    entry = _entry("CR", "funding", 0)
    first_page = WalletStatementPage(entries=[entry], next_cursor="older")
    last_page = WalletStatementPage(entries=[], next_cursor=None)
    client = FakeWallet(_balance(), [first_page, last_page, first_page, last_page])

    first = _run_reconcile(client)
    second = _run_reconcile(client)

    assert client.calls == [None, "older", None, "older"]
    assert first.explanation["statement_entries_new"] == 1
    assert second.explanation["statement_entries_new"] == 0


def test_statement_page_limit_and_repeated_cursor_are_reported():
    client = FakeWallet(_balance(), [
        WalletStatementPage(entries=[], next_cursor="page-2"),
        WalletStatementPage(entries=[], next_cursor="page-3"),
    ])
    with pytest.raises(WalletError, match="máximo de páginas"):
        asyncio.run(fetch_statement_entries(client, max_pages=2))
    assert client.calls == [None, "page-2"]

    repeated = FakeWallet(_balance(), [
        WalletStatementPage(entries=[], next_cursor="same"),
        WalletStatementPage(entries=[], next_cursor="same"),
    ])
    with pytest.raises(WalletError, match="repitió el mismo nextCursor"):
        asyncio.run(fetch_statement_entries(repeated))


def test_page_limit_does_not_persist_partial_reconciliation(monkeypatch):
    monkeypatch.setattr(wallet_reconciliation, "_last_tick_monotonic", None)

    class EndlessWallet:
        def __init__(self):
            self.pages = 0
            self.balances = 0

        async def get_balance(self):
            self.balances += 1
            return _balance()

        async def get_statement(self, cycle_id=None, cursor=None):
            self.pages += 1
            return WalletStatementPage(entries=[], next_cursor=f"page-{self.pages}")

    client = EndlessWallet()
    result = asyncio.run(reconcile_wallet_tick(interval_seconds=60, client=client, now=NOW))

    assert result is None
    assert client.balances == 1
    assert client.pages == 100
    with Session(engine) as session:
        assert session.exec(select(WalletReconciliation)).all() == []
        assert session.exec(select(WalletBalanceSnapshot)).all() == []


def test_reconcile_tick_currently_retries_before_wallet_retry_after(monkeypatch):
    """Documenta el riesgo de 429; no cambia la política en una tarjeta de QA."""
    clock = [100.0]
    monkeypatch.setattr(wallet_reconciliation, "_last_tick_monotonic", None)
    monkeypatch.setattr(wallet_reconciliation, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    class LimitedWallet:
        calls = 0

        async def get_balance(self):
            self.calls += 1
            raise WalletRateLimitError("rate limited", status_code=429, retry_after=120)

    client = LimitedWallet()
    assert asyncio.run(reconcile_wallet_tick(interval_seconds=60, client=client, now=NOW)) is None
    clock[0] = 161.0
    assert asyncio.run(reconcile_wallet_tick(interval_seconds=60, client=client, now=NOW)) is None
    assert client.calls == 2  # El Retry-After de 120 s no se aplicó.
