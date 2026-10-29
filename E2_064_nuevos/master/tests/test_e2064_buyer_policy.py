"""E2-064 / RF03 / AD6: compras por precio efectivo, rutas, span y wallet.

Casos de integración con PostgreSQL real y el outbox existente E2-038.
La fixture global conftest.py usa exclusivamente energyshark_test_*.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlmodel import Session, select

from app.database import engine
from app.models import (
    Cycle, DistanceTable, MarketBid, MarketCycle, MarketFeedOffer,
    OutboundMessage, StrategyConfig,
)
from app.services.autonomous_buy import autonomous_buy_tick
from app.services.offer_selection import get_buy_candidates


class FakeWallet:
    def __init__(self, available: str, credit_line: str = "0") -> None:
        self.available = Decimal(available)
        self.credit_line = Decimal(credit_line)

    async def get_balance(self):
        return SimpleNamespace(
            cr_available=self.available,
            credit_line=self.credit_line,
        )


def seed(
    session: Session, *, now: datetime, deficit: int = 10,
    span: int = 0, policy: str = "CHEAPEST_EFFECTIVE_PRICE",
    min_bid: int = 2, imbalance_price: str | None = "1",
    opens_at: datetime | None = None, last_bid_at: datetime | None = None,
    closed: bool = False, disabled_cost: str | None = "20",
    routes: dict | None = None,
):
    cid = f"e2064-{uuid4()}"
    opens = opens_at or now - timedelta(minutes=2)
    last_bid = last_bid_at or now + timedelta(minutes=5)
    market = {"minBid": min_bid}
    if imbalance_price is not None:
        market["imbalancePrice"] = imbalance_price

    session.add(StrategyConfig(
        version=1, status="ACTIVE", span_seconds=span,
        offer_selection_policy=policy, bid_acceptance_policy="FIFO",
        offer_price_markup=Decimal("1.2"),
        offer_quantity_fraction=Decimal("1"),
        created_at=now - timedelta(minutes=10),
    ))
    session.flush()
    session.add(Cycle(
        cycle_id=cid, created_at=now,
        status_idpk=str(uuid4()), status_payload={"market": market},
        market_opens_at=opens, market_last_bid_at=last_bid,
        market_closes_at=now + timedelta(minutes=6),
        strategy_version=1,
        generation_capacity=Decimal("0"),
        consumption=Decimal(deficit),
    ))
    session.add(MarketCycle(
        cycle_id=cid,
        closed_at=(now - timedelta(seconds=1)) if closed else None,
    ))
    session.flush()
    session.add(DistanceTable(
        msg_id=str(uuid4()), idpk=str(uuid4()), cycle_id=cid,
        source_timestamp=now - timedelta(seconds=5), received_at=now,
        disabled_route_transport_cost=(
            Decimal(disabled_cost) if disabled_cost is not None else None
        ),
        distances=routes or {
            "HGW": {"distance": 1000, "transportCost": "1", "enabled": True},
            "COR": {"distance": 2000, "transportCost": "2", "enabled": True},
            "TAR": {"distance": 1000, "transportCost": "1", "enabled": False},
        },
    ))
    session.flush()
    return cid


def offer(session: Session, cid: str, city: str, price: str, remaining: int, now: datetime) -> str:
    offer_id = f"{city}-{uuid4()}"
    session.add(MarketFeedOffer(
        cycle_id=cid, city_id=city, offer_id=offer_id,
        initial_quantity=remaining, remaining=remaining,
        price_per_energy=Decimal(price), state="ACTIVE", uncertain=False,
        last_event_at=now - timedelta(seconds=10), last_event_msg_id=str(uuid4()),
    ))
    session.flush()
    return offer_id


@pytest.mark.parametrize("imbalance", ["1", None])
def test_no_price_cap_from_e1_or_imbalance_price(imbalance):
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        cid = seed(session, now=now, imbalance_price=imbalance, deficit=10)
        oid = offer(session, cid, "HGW", "150", 10, now)  # efectivo 151 > cap E1
        session.commit()
        stats = asyncio.run(autonomous_buy_tick(
            session, cycle_id=cid, now=now, client=FakeWallet("2000")
        ))
        bids = session.exec(select(MarketBid).where(MarketBid.cycle_id == cid)).all()
        assert stats["bids_created"] == 1 and stats["quantity_bid"] == 10
        assert stats["skipped_price"] == 0
        assert len(bids) == 1 and bids[0].offer_id == oid
        assert bids[0].quantity == 10
        out = session.exec(select(OutboundMessage).where(
            OutboundMessage.cycle_id == cid, OutboundMessage.message_type == "bid"
        )).one()
        assert out.payload["data"] == {"offerId": oid, "quantity": 10}
        assert "price" not in str(out.payload).lower()


def test_prefers_effective_price_not_nominal_and_applies_disabled_penalty():
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        cid = seed(session, now=now, deficit=5)
        a = offer(session, cid, "HGW", "10", 10, now)  # efectivo 11
        offer(session, cid, "COR", "9", 10, now)       # efectivo 13
        offer(session, cid, "TAR", "1", 10, now)       # disabled: 1+20=21
        candidates = get_buy_candidates(session, cycle_id=cid, now=now)
        assert [str(x.effectivePricePerEnergy) for x in candidates.candidates] == [
            "11.00", "13.00", "21.00",
        ]
        assert candidates.candidates[-1].routeEnabled is False
        session.commit()
        stats = asyncio.run(autonomous_buy_tick(
            session, cycle_id=cid, now=now, client=FakeWallet("1000")
        ))
        assert stats["bids_created"] == 1
        bid = session.exec(select(MarketBid).where(MarketBid.cycle_id == cid)).one()
        assert bid.offer_id == a


def test_min_distance_policy_applies_even_if_more_expensive():
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        cid = seed(
            session, now=now, policy="MIN_DISTANCE", deficit=6,
            routes={
                "HGW": {"distance": 3000, "transportCost": "1", "enabled": True},
                "COR": {"distance": 500, "transportCost": "1", "enabled": True},
            },
        )
        offer(session, cid, "HGW", "1", 10, now)  # efectivo 4, lejos
        near = offer(session, cid, "COR", "30", 10, now)  # efectivo 30.50, cerca
        session.commit()
        stats = asyncio.run(autonomous_buy_tick(
            session, cycle_id=cid, now=now, client=FakeWallet("1000")
        ))
        assert stats["bids_created"] == 1
        assert session.exec(select(MarketBid).where(
            MarketBid.cycle_id == cid
        )).one().offer_id == near


def test_disabled_route_allowed_with_penalty_but_not_without_official_rate():
    now = datetime.now(timezone.utc)
    routes = {"TAR": {"distance": 1000, "transportCost": "0.01", "enabled": False}}
    with Session(engine) as session:
        cid = seed(session, now=now, disabled_cost=None, routes=routes)
        offer(session, cid, "TAR", "10", 10, now)
        assert get_buy_candidates(session, cycle_id=cid, now=now).candidates == []
        session.commit()
        stats = asyncio.run(autonomous_buy_tick(
            session, cycle_id=cid, now=now, client=FakeWallet("1000")
        ))
        assert stats["bids_created"] == 0


def test_wallet_capacity_limits_quantity_and_respects_minbid():
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        cid = seed(session, now=now, deficit=10, min_bid=5, routes={
            "HGW": {"distance": 1000, "transportCost": "0", "enabled": True}
        })
        offer(session, cid, "HGW", "30", 10, now)
        session.commit()
        too_little = asyncio.run(autonomous_buy_tick(
            session, cycle_id=cid, now=now, client=FakeWallet("100")
        ))
        assert too_little["bids_created"] == 0
        assert too_little["skipped_capacity"] == 1
        enough_credit = asyncio.run(autonomous_buy_tick(
            session, cycle_id=cid, now=now, client=FakeWallet("100", "50")
        ))
        assert enough_credit["bids_created"] == 1
        assert session.exec(select(MarketBid).where(
            MarketBid.cycle_id == cid
        )).one().quantity == 5


def test_span_applies_only_on_cycle_policy_and_not_pending_edits():
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        cid = seed(session, now=now, deficit=5, span=120,
                   opens_at=now - timedelta(seconds=30))
        offer(session, cid, "HGW", "10", 10, now)
        session.add(StrategyConfig(
            version=2, status="PENDING", span_seconds=0,
            offer_selection_policy="MIN_DISTANCE", created_at=now,
        ))
        session.commit()
        before = get_buy_candidates(session, cycle_id=cid, now=now)
        assert before.strategyVersion == 1
        assert before.spanSeconds == 120
        assert before.reason == "WAITING_SPAN"
        first = asyncio.run(autonomous_buy_tick(
            session, cycle_id=cid, now=now, client=FakeWallet("1000")
        ))
        assert first["bids_created"] == 0
        after = asyncio.run(autonomous_buy_tick(
            session, cycle_id=cid, now=now + timedelta(seconds=91),
            client=FakeWallet("1000")
        ))
        assert after["bids_created"] == 1


def test_last_bid_and_market_closed_block_purchase():
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        cid = seed(session, now=now, last_bid_at=now, deficit=8)
        offer(session, cid, "HGW", "10", 10, now)
        session.commit()
        assert get_buy_candidates(session, cycle_id=cid, now=now).canBid is False
        assert asyncio.run(autonomous_buy_tick(
            session, cycle_id=cid, now=now, client=FakeWallet("1000")
        ))["bids_created"] == 0
    with Session(engine) as session:
        # El primer ciclo tiene strategy.version=1; fixture limpia por test,
        # por lo que continuamos en el mismo ciclo y solo cambiamos su cierre.
        market = session.get(MarketCycle, cid)
        market.closed_at = now - timedelta(seconds=1)
        session.add(market)
        session.commit()
        assert get_buy_candidates(session, cycle_id=cid, now=now).reason == "MARKET_CLOSED"


def test_pending_bid_blocks_new_tick_until_answer_and_no_double_purchase():
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        cid = seed(session, now=now, deficit=10)
        offer(session, cid, "HGW", "12", 6, now)
        offer(session, cid, "COR", "12", 6, now)
        session.commit()
        first = asyncio.run(autonomous_buy_tick(
            session, cycle_id=cid, now=now, client=FakeWallet("1000"), max_bids=1
        ))
        assert first["bids_created"] == 1
        second = asyncio.run(autonomous_buy_tick(
            session, cycle_id=cid, now=now, client=FakeWallet("1000"), max_bids=5
        ))
        assert second["bids_created"] == 0
        assert second["skipped_pending"] == 1
        assert len(session.exec(select(MarketBid).where(
            MarketBid.cycle_id == cid
        )).all()) == 1
