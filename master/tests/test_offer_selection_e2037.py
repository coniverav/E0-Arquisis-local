"""E2-037: selección pura desde feed, transportes, minBid, tiempo y AD6."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.database import engine, run_migrations
from app.main import app
from app.models import (
    Cycle, DistanceTable, MarketCycle, MarketFeedOffer, OutboundMessage, StrategyConfig,
)
from app.services.offer_selection import get_buy_candidates

NOW = datetime(2026, 10, 26, 12, 3, tzinfo=timezone.utc)


def setup_module():
    run_migrations()


def _setup(session, *, policy="CHEAPEST_EFFECTIVE_PRICE", span=60, min_bid=3,
           opened=None, last_bid=None, closes=None, status=True, strategy=True,
           table=True, market_closed=False):
    cid = f"e2037-{uuid4()}"
    opened = NOW - timedelta(minutes=2) if opened is None else opened
    last_bid = NOW + timedelta(minutes=4) if last_bid is None else last_bid
    closes = NOW + timedelta(minutes=5) if closes is None else closes
    version = (uuid4().int % 1000000000) + 1000000000
    if strategy:
        session.add(StrategyConfig(
            version=version, status="ACTIVE", offer_price_markup=Decimal("1.2"),
            offer_quantity_fraction=Decimal("1"), bid_acceptance_policy="FIFO",
            offer_selection_policy=policy, span_seconds=span, created_at=NOW,
        ))
        session.flush()

    session.add(Cycle(
        cycle_id=cid, created_at=NOW,
        status_idpk=str(uuid4()) if status else None,
        status_payload={"market": {"minBid": min_bid}} if status else {},
        market_opens_at=opened, market_last_bid_at=last_bid,
        market_closes_at=closes, strategy_version=version if strategy else None,
    ))
    session.add(MarketCycle(
        cycle_id=cid, closed_at=NOW - timedelta(seconds=1) if market_closed else None,
    ))
    session.flush()
    if table:
        session.add(DistanceTable(
            msg_id=str(uuid4()), idpk=str(uuid4()), cycle_id=cid,
            source_timestamp=NOW - timedelta(seconds=5), received_at=NOW,
            disabled_route_transport_cost=Decimal("5"),
            distances={
                "HGW": {"distance": 1000, "transportCost": 1, "enabled": True},
                "COR": {"distance": 3000, "transportCost": 1, "enabled": True},
                "TAR": {"distance": 2000, "transportCost": 1, "enabled": False},
                "KLD": {"distance": 1000, "transportCost": 1, "enabled": True},
            },
        ))
    session.flush()
    return cid, version


def _offer(session, cid, city="HGW", *, price="10", remaining=20,
           state="ACTIVE", uncertain=False):
    session.add(MarketFeedOffer(
        cycle_id=cid, city_id=city, offer_id=f"{city}-{uuid4()}",
        initial_quantity=20, remaining=remaining,
        price_per_energy=Decimal(price), state=state, uncertain=uncertain,
        last_event_at=NOW - timedelta(seconds=10), last_event_msg_id=str(uuid4()),
    ))
    session.flush()


def test_cheap_effective_price_overrides_nominal_order():
    with Session(engine) as s:
        cid, ver = _setup(s)
        _offer(s, cid, "HGW", price="10")  # efectivo 11
        _offer(s, cid, "COR", price="9")   # efectivo 12
        result = get_buy_candidates(s, cycle_id=cid, now=NOW)
        assert result.canBid and result.reason is None
        assert result.strategyVersion == ver
        assert result.selectionPolicy == "CHEAPEST_EFFECTIVE_PRICE"
        assert [o.cityId for o in result.candidates] == ["HGW", "COR"]
        assert result.candidates[0].effectivePricePerEnergy == Decimal("11.00")
        assert result.minBid == 3
        s.rollback()


def test_distance_policy_overrides_effective_price():
    with Session(engine) as s:
        cid, _ = _setup(s, policy="MIN_DISTANCE")
        _offer(s, cid, "HGW", price="50")
        _offer(s, cid, "COR", price="1")
        result = get_buy_candidates(s, cycle_id=cid, now=NOW)
        assert [o.cityId for o in result.candidates] == ["HGW", "COR"]
        s.rollback()


def test_disabled_route_is_candidate_with_official_penalty():
    with Session(engine) as s:
        cid, _ = _setup(s)
        _offer(s, cid, "TAR", price="1")
        result = get_buy_candidates(s, cycle_id=cid, now=NOW)
        assert result.total == 1
        assert result.candidates[0].routeEnabled is False
        assert result.candidates[0].effectivePricePerEnergy == Decimal("11.00")
        s.rollback()


def test_min_bid_requires_sufficient_remaining():
    with Session(engine) as s:
        cid, _ = _setup(s, min_bid=5)
        _offer(s, cid, "HGW", remaining=4)
        _offer(s, cid, "COR", remaining=5)
        result = get_buy_candidates(s, cycle_id=cid, now=NOW)
        assert [o.cityId for o in result.candidates] == ["COR"]
        s.rollback()


def test_never_suggest_own_city_uncertain_or_withdrawn():
    with Session(engine) as s:
        cid, _ = _setup(s)
        _offer(s, cid, "KLD")  # CITY_ID local
        _offer(s, cid, "COR", uncertain=True)
        _offer(s, cid, "HGW", state="WITHDRAWN", remaining=0)
        assert get_buy_candidates(s, cycle_id=cid, now=NOW).total == 0
        s.rollback()


def test_missing_distance_table_is_not_eligible():
    with Session(engine) as s:
        cid, _ = _setup(s, table=False)
        _offer(s, cid)
        assert get_buy_candidates(s, cycle_id=cid, now=NOW).candidates == []
        s.rollback()


@pytest.mark.parametrize("opened,span,last_bid,closes,expected", [
    (NOW + timedelta(minutes=1), 0, NOW + timedelta(minutes=3), NOW + timedelta(minutes=5), "MARKET_NOT_OPEN"),
    (NOW - timedelta(seconds=5), 60, NOW + timedelta(minutes=3), NOW + timedelta(minutes=5), "WAITING_SPAN"),
    (NOW - timedelta(minutes=3), 0, NOW, NOW + timedelta(minutes=1), "LAST_BID_PASSED"),
    (NOW - timedelta(minutes=3), 0, NOW - timedelta(minutes=1), NOW, "MARKET_CLOSED"),
])
def test_time_windows_fail_closed(opened, span, last_bid, closes, expected):
    with Session(engine) as s:
        cid, _ = _setup(s, opened=opened, span=span, last_bid=last_bid, closes=closes)
        _offer(s, cid)
        result = get_buy_candidates(s, cycle_id=cid, now=NOW)
        assert result.canBid is False and result.reason == expected
        assert result.candidates == []
        s.rollback()


def test_min_bid_missing_or_invalid_never_guessed():
    with Session(engine) as s:
        cid, _ = _setup(s, min_bid=True)
        _offer(s, cid)
        result = get_buy_candidates(s, cycle_id=cid, now=NOW)
        assert result.reason == "MISSING_MIN_BID" and result.candidates == []
        s.rollback()


def test_absent_status_or_ad6_strategy_never_selects():
    with Session(engine) as s:
        cid, _ = _setup(s, status=False)
        _offer(s, cid)
        assert get_buy_candidates(s, cycle_id=cid, now=NOW).reason == "MISSING_STATUS"
        cid2, _ = _setup(s, strategy=False)
        _offer(s, cid2)
        assert get_buy_candidates(s, cycle_id=cid2, now=NOW).reason == "MISSING_CYCLE_STRATEGY"
        s.rollback()


def test_explicit_market_closed_blocks_candidates():
    with Session(engine) as s:
        cid, _ = _setup(s, market_closed=True)
        _offer(s, cid)
        result = get_buy_candidates(s, cycle_id=cid, now=NOW)
        assert result.reason == "MARKET_CLOSED" and result.candidates == []
        s.rollback()


def test_pinned_strategy_not_replaced_by_newer_pending_version():
    with Session(engine) as s:
        cid, version = _setup(s, policy="MIN_DISTANCE")
        s.add(StrategyConfig(
            version=version + 1, status="PENDING",
            offer_selection_policy="CHEAPEST_EFFECTIVE_PRICE", span_seconds=0,
            created_at=NOW,
        ))
        _offer(s, cid)
        result = get_buy_candidates(s, cycle_id=cid, now=NOW)
        assert result.strategyVersion == version
        assert result.selectionPolicy == "MIN_DISTANCE"
        s.rollback()


def test_http_endpoint_is_readonly():
    with Session(engine) as s:
        now = datetime.now(timezone.utc)
        cid, _ = _setup(
            s, opened=now - timedelta(minutes=2), last_bid=now + timedelta(minutes=3),
            closes=now + timedelta(minutes=4), span=0,
        )
        _offer(s, cid)
        s.commit()
    response = TestClient(app).get("/market/buy-candidates", params={"cycleId": cid})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["canBid"] is True and result["total"] == 1
    assert result["candidates"][0]["cityId"] == "HGW"
    with Session(engine) as s:
        assert s.exec(select(OutboundMessage).where(
            OutboundMessage.cycle_id == cid
        )).all() == []


def test_unknown_cycle_no_side_effects():
    with Session(engine) as s:
        result = get_buy_candidates(s, cycle_id=f"missing-{uuid4()}", now=NOW)
        assert result.reason == "MISSING_STATUS" and result.total == 0
        assert result.strategyVersion is None
        s.rollback()
