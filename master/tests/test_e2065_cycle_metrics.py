"""E2-065 / RF06: snapshots persistentes, baseline y comparación AD6."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import Barrier
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.database import engine
from app.main import app
from app.models import (
    Cycle, CycleStrategyMetric, ImbalanceSettlement, LedgerEntry,
    MarketBid, MarketOffer, StrategyConfig,
)
from app.services.cycle_metrics import refresh_cycle_metrics, refresh_recent_cycle_metrics

NOW = datetime.now(timezone.utc).replace(microsecond=0)
client = TestClient(app)


def _seed(session, *, closed=False, price="5", span=45, selection="MIN_DISTANCE"):
    cycle_id = f"e2065-{uuid4()}"
    version = uuid4().int % 1_000_000_000 + 1_000_000_000
    session.add(StrategyConfig(
        version=version, status="ACTIVE", created_at=NOW - timedelta(minutes=5),
        span_seconds=span, offer_selection_policy=selection,
        bid_acceptance_policy="FIFO", offer_price_markup=Decimal("1.2"),
        offer_quantity_fraction=Decimal("1"),
    ))
    session.flush()
    session.add(Cycle(
        cycle_id=cycle_id, created_at=NOW - timedelta(minutes=3),
        status_idpk=str(uuid4()), strategy_version=version,
        status_payload={"market": {"imbalancePrice": price}},
        generation_capacity=Decimal("1000"), consumption=Decimal("1500"),
        market_opens_at=NOW - timedelta(minutes=2),
        market_last_bid_at=NOW + timedelta(minutes=4),
        market_closes_at=NOW - timedelta(seconds=1) if closed else NOW + timedelta(minutes=5),
    ))
    session.flush()
    return cycle_id, version


def _entry(session, cycle_id, seq, operation, budget, energy="0", details=None, idpk=None):
    session.add(LedgerEntry(
        cycle_id=cycle_id, sequence_no=seq,
        operation_type=operation, idpk=idpk or str(uuid4()),
        budget_delta=Decimal(budget), energy_delta=Decimal(energy),
        budget_after=Decimal("0"), energy_after=Decimal("0"),
        details=details or {}, applied_at=NOW,
    ))
    session.flush()


def _trade_swap(session, cycle_id, swap_id="swp-1"):
    _entry(session, cycle_id, 1, "SWAP_SETTLED", "-600", "300", {
        "swapId": swap_id, "role": "ACCEPTOR",
        "give": {"asset": "CR", "amount": "600"},
        "take": {"asset": "KWH", "amount": "300"},
    }, idpk=swap_id)


def _settlement(session, cycle_id, *, price="5", total="-1000"):
    settlement_id = str(uuid4())
    session.add(ImbalanceSettlement(
        idpk=str(uuid4()), msg_id=str(uuid4()), cycle_id=cycle_id,
        settlement_id=settlement_id,
        kwh_held=300, deficit=500, coverage=-200,
        price_per_energy=Decimal(price), total=Decimal(total),
        balance_after=Decimal("0"), recorded_at=NOW,
    ))
    _entry(session, cycle_id, 3, "IMBALANCE_SETTLEMENT", total,
           details={"settlementId": settlement_id}, idpk=settlement_id)


def test_records_span_version_rejected_bids_coverage_and_baseline():
    with Session(engine) as session:
        cid, version = _seed(session)
        oid = f"HGW-{cid}"
        session.add(MarketOffer(
            offer_id=oid, cycle_id=cid, city_id="HGW", is_local=False,
            initial_quantity=200, remaining_quantity=200,
            price_per_energy=Decimal("3"), status="ACTIVE",
            published_at=NOW, last_updated_at=NOW,
        ))
        session.flush()
        session.add(MarketBid(
            idpk=str(uuid4()), msg_id=str(uuid4()), cycle_id=cid,
            offer_id=oid, counterpart_city_id="HGW", direction="OUTGOING",
            quantity=20, status="REJECTED", rejection_reason="OFFER_WITHDRAWN",
            created_at=NOW,
        ))
        session.flush()
        value = refresh_cycle_metrics(session, cycle_id=cid, now=NOW)
        assert value.strategy_version == version
        assert value.span_seconds == 45
        assert value.selection_policy == "MIN_DISTANCE"
        assert value.deficit_kwh == 500
        assert value.rejected_bids == 1
        assert value.covered_kwh == 0
        assert value.coverage_rate == Decimal("0.0000")
        assert value.baseline_cost == Decimal("2500.00")
        assert value.baseline_source == "STATUS_ESTIMATE"
        assert value.real_cost is None
        assert value.status == "IN_PROGRESS"
        session.commit()

    response = client.get("/market/cycle-metrics", params={"cycleId": cid})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["strategyVersion"] == version
    assert body["spanSeconds"] == 45
    assert body["rejectedBids"] == 1
    assert body["baselineCost"] == "2500.00"
    assert body["realCost"] is None


def test_cost_real_requires_settlement_and_confirmed_transport():
    with Session(engine) as session:
        cid, version = _seed(session, closed=True, price="100")
        _trade_swap(session, cid)
        _settlement(session, cid, price="5")
        metric = refresh_cycle_metrics(session, cycle_id=cid, now=NOW)
        assert metric.status == "AWAITING_TRANSPORT"
        assert metric.missing_transport_trades == 1
        assert metric.buy_cost == Decimal("600")
        assert metric.settlement_cost == Decimal("1000")
        assert metric.real_cost is None
        # No atribuir savings hasta que lleguen todos los cargos.
        assert metric.savings_vs_baseline is None
        session.flush()
        _entry(session, cid, 2, "SWAP_TRANSPORT", "-90", details={
            "role": "BUYER", "swapId": "swp-1", "transportTotal": "90",
        }, idpk="swp-1")
        metric = refresh_cycle_metrics(session, cycle_id=cid, now=NOW)
        assert metric.status == "FINAL"
        assert metric.bought_kwh == 300
        assert metric.covered_kwh == 300
        assert metric.coverage_rate == Decimal("0.6000")
        assert metric.buy_cost == Decimal("600")
        assert metric.transport_cost == Decimal("90")
        assert metric.settlement_cost == Decimal("1000")
        assert metric.real_cost == Decimal("1690")
        # Precio oficial de cierre reemplaza la estimación previa (100).
        assert metric.baseline_source == "SETTLEMENT"
        assert metric.baseline_cost == Decimal("2500")
        assert metric.savings_vs_baseline == Decimal("810")
        session.commit()

    body = client.get("/market/cycle-metrics", params={"cycleId": cid}).json()
    assert body["status"] == "FINAL"
    assert body["realCost"] == "1690.00"
    assert body["savingsVsBaseline"] == "810.00"


def test_missing_settlement_keeps_real_cost_null_after_market_close():
    with Session(engine) as session:
        cid, _ = _seed(session, closed=True)
        metric = refresh_cycle_metrics(session, cycle_id=cid, now=NOW)
        assert metric.status == "AWAITING_SETTLEMENT"
        assert metric.baseline_cost == Decimal("2500")
        assert metric.real_cost is None
        session.commit()


def test_duplicate_refresh_does_not_duplicate_or_modify_unchanged_timestamp():
    with Session(engine) as session:
        cid, _ = _seed(session)
        first = refresh_cycle_metrics(session, cycle_id=cid, now=NOW)
        session.commit()
        saved = first.updated_at
        second = refresh_cycle_metrics(session, cycle_id=cid, now=NOW + timedelta(seconds=15))
        session.commit()
        assert second.updated_at == saved
        assert len(session.exec(select(CycleStrategyMetric).where(
            CycleStrategyMetric.cycle_id == cid)).all()) == 1


def test_excludes_seller_income_and_does_not_count_unsettled_bids():
    with Session(engine) as session:
        cid, _ = _seed(session, closed=True)
        _entry(session, cid, 1, "SWAP_SETTLED", "600", "-300", {
            "role": "PROPOSER", "give": {"asset": "KWH", "amount": "300"},
            "take": {"asset": "CR", "amount": "600"},
        })
        metric = refresh_cycle_metrics(session, cycle_id=cid, now=NOW)
        assert metric.bought_kwh == 0
        assert metric.buy_cost == Decimal("0.00")
        session.rollback()


def test_compare_endpoint_is_readonly_and_404_unknown():
    with Session(engine) as session:
        cid, _ = _seed(session)
        refresh_cycle_metrics(session, cycle_id=cid, now=NOW)
        session.commit()
    result = client.get("/market/cycle-metrics/compare", params={"limit": 10})
    assert result.status_code == 200, result.text
    assert result.json()["total"] == 1
    assert result.json()["items"][0]["cycleId"] == cid
    assert client.get("/market/cycle-metrics/compare", params={"limit": 201}).status_code == 422
    assert client.get("/market/cycle-metrics", params={"cycleId": "no-existe"}).status_code == 404


def test_scheduler_backfills_current_cycle_without_mutating_ledger():
    with Session(engine) as session:
        cid, _ = _seed(session)
        assert refresh_recent_cycle_metrics(session, now=NOW, limit=10) == 1
        assert session.get(CycleStrategyMetric, cid) is not None
        assert session.exec(select(LedgerEntry)).all() == []
        session.rollback()


def test_two_workers_refresh_one_row_using_cycle_lock():
    with Session(engine) as session:
        cid, _ = _seed(session)
        session.commit()
    barrier = Barrier(2)

    def run():
        barrier.wait(timeout=10)
        with Session(engine) as session:
            metric = refresh_cycle_metrics(session, cycle_id=cid, now=NOW)
            session.commit()
            return metric.strategy_version

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(run)
        b = pool.submit(run)
        assert a.result(timeout=20) == b.result(timeout=20)
    with Session(engine) as session:
        assert len(session.exec(select(CycleStrategyMetric)).all()) == 1
