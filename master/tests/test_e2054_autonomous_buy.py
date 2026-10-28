"""E2-054: compra autónoma limitada por déficit, wallet y precio."""

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from sqlmodel import Session, select

from app.database import engine
from app.models import Cycle, MarketBid
from app.schemas import MarketBookOfferOut, MarketBuyCandidatesOut
from app.services import autonomous_buy
from app.services import buy_bids as buy_bids_module


class FakeWallet:
    def __init__(self, available: str, credit_line: str = "0"):
        self.available = Decimal(available)
        self.credit_line = Decimal(credit_line)

    async def get_balance(self):
        return SimpleNamespace(
            cr_available=self.available,
            credit_line=self.credit_line,
        )


def _candidate(cycle_id, offer_id, remaining, price, now):
    return MarketBookOfferOut(
        cycleId=cycle_id,
        cityId="MET",
        offerId=offer_id,
        quantity=remaining,
        remaining=remaining,
        pricePerEnergy=Decimal(price),
        lastEventAt=now,
        lastEventMsgId=str(uuid4()),
        state="ACTIVE",
        uncertain=False,
        stale=False,
        distanceMeters=Decimal("100"),
        routeEnabled=True,
        transportPerEnergy=Decimal("0"),
        effectivePricePerEnergy=Decimal(price),
    )


def _context(session, *, cycle_id, now, candidates):
    session.add(Cycle(
        cycle_id=cycle_id,
        status_idpk=str(uuid4()),
        status_payload={"market": {"imbalancePrice": 100}},
        generation_capacity=Decimal("1000"),
        consumption=Decimal("1500"),
        opening_budget_balance=Decimal("0"),
        opening_energy_balance=Decimal("0"),
        budget_balance=Decimal("0"),
        energy_balance=Decimal("0"),
        last_sequence=0,
        created_at=now,
        market_opens_at=now.replace(tzinfo=timezone.utc),
        market_last_bid_at=now.replace(tzinfo=timezone.utc),
    ))
    session.commit()

    result = MarketBuyCandidatesOut(
        cycleId=cycle_id,
        asOf=now,
        selectionPolicy="CHEAPEST_EFFECTIVE_PRICE",
        minBid=100,
        marketClosed=False,
        canBid=True,
        total=len(candidates),
        candidates=candidates,
    )
    return result


def test_autonomous_buy_covers_deficit_and_skips_expensive_offers(monkeypatch):
    now = datetime.now(timezone.utc)
    cycle_id = f"e2054-{uuid4()}"
    with Session(engine) as session:
        candidates = _context(
            session,
            cycle_id=cycle_id,
            now=now,
            candidates=[
                _candidate(cycle_id, "O3", 400, "150", now),
                _candidate(cycle_id, "O1", 300, "50", now),
                _candidate(cycle_id, "O2", 400, "60", now),
            ],
        )
        monkeypatch.setattr(
            autonomous_buy, "get_buy_candidates", lambda *a, **k: candidates
        )
        monkeypatch.setattr(
            buy_bids_module, "get_buy_candidates", lambda *a, **k: candidates
        )

        stats = asyncio.run(autonomous_buy.autonomous_buy_tick(
            session,
            cycle_id=cycle_id,
            now=now,
            client=FakeWallet("50000"),
        ))

        assert stats["deficit"] == 500
        assert stats["bids_created"] == 2
        assert stats["quantity_bid"] == 500
        assert stats["skipped_price"] == 1

        bids = session.exec(
            select(MarketBid).where(MarketBid.cycle_id == cycle_id)
        ).all()
        assert {bid.offer_id for bid in bids} == {"O1", "O2"}
        assert sum(bid.quantity for bid in bids) == 500
        assert {bid.status for bid in bids} == {"PENDING"}


def test_autonomous_buy_trims_quantity_to_wallet_capacity(monkeypatch):
    now = datetime.now(timezone.utc)
    cycle_id = f"e2054-{uuid4()}"
    with Session(engine) as session:
        candidates = _context(
            session,
            cycle_id=cycle_id,
            now=now,
            candidates=[_candidate(cycle_id, "O1", 300, "50", now)],
        )
        monkeypatch.setattr(
            autonomous_buy, "get_buy_candidates", lambda *a, **k: candidates
        )
        monkeypatch.setattr(
            buy_bids_module, "get_buy_candidates", lambda *a, **k: candidates
        )

        stats = asyncio.run(autonomous_buy.autonomous_buy_tick(
            session,
            cycle_id=cycle_id,
            now=now,
            client=FakeWallet("12000", credit_line="0"),
        ))

        assert stats["quantity_bid"] == 240
        bid = session.exec(
            select(MarketBid).where(MarketBid.cycle_id == cycle_id)
        ).one()
        assert bid.quantity == 240
