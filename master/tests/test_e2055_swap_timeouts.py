"""E2-055: timeouts liberan holds solo con evidencia y expiran bids."""

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from sqlmodel import Session, select

from app.database import engine
from app.models import (
    Cycle,
    MarketBid,
    MarketOffer,
    OfferReservation,
    SwapRecord,
)
from app.services.swap_timeouts import expire_stale_swaps


class FakeWallet:
    def __init__(self, statuses=None):
        self.statuses = statuses or {}
        self.get_calls = []
        self.cancel_calls = []

    async def get_swap(self, swap_id):
        self.get_calls.append(swap_id)
        return {"swapId": swap_id, "status": self.statuses.get(swap_id, "proposed")}

    async def cancel_swap(self, swap_id, *, idempotency_key):
        self.cancel_calls.append(swap_id)


def _context(session, *, cycle_id, offer_id, now, last_bid_at=None):
    session.add(Cycle(
        cycle_id=cycle_id,
        status_idpk=str(uuid4()),
        status_payload={"market": {}},
        opening_budget_balance=Decimal("0"),
        opening_energy_balance=Decimal("0"),
        budget_balance=Decimal("0"),
        energy_balance=Decimal("0"),
        last_sequence=0,
        created_at=now,
        market_last_bid_at=last_bid_at,
    ))
    session.add(MarketOffer(
        offer_id=offer_id,
        cycle_id=cycle_id,
        city_id="KLD",
        is_local=True,
        initial_quantity=1000,
        remaining_quantity=1000,
        price_per_energy=Decimal("10"),
        status="ACTIVE",
        published_at=now,
        last_updated_at=now,
    ))
    session.commit()


def _proposer(session, *, cycle_id, offer_id, bid_idpk, now, **kwargs):
    session.add(MarketBid(
        idpk=bid_idpk,
        msg_id=str(uuid4()),
        cycle_id=cycle_id,
        direction="INCOMING",
        offer_id=offer_id,
        counterpart_city_id="COR",
        quantity=100,
        status="RESERVED",
        created_at=now,
    ))
    session.add(OfferReservation(
        offer_id=offer_id,
        bid_idpk=bid_idpk,
        quantity=100,
        status="HOLD",
        created_at=now,
    ))
    record = SwapRecord(
        idpk=bid_idpk,
        cycle_id=cycle_id,
        role="PROPOSER",
        counterpart_city_id="COR",
        give_asset="KWH",
        give_amount=Decimal("100"),
        take_asset="CR",
        take_amount=Decimal("1000"),
        memo=offer_id,
        status=kwargs.pop("status", "SUBMITTED"),
        idempotency_key=str(uuid4()),
        created_at=now,
        **kwargs,
    )
    session.add(record)
    session.commit()
    return record


def test_proposal_never_sent_expires_without_wallet_call():
    now = datetime.now(timezone.utc)
    cycle_id = f"e2055-{uuid4()}"
    offer_id = f"KLD-{cycle_id}"
    bid_idpk = str(uuid4())

    with Session(engine) as session:
        _context(session, cycle_id=cycle_id, offer_id=offer_id, now=now)
        _proposer(
            session, cycle_id=cycle_id, offer_id=offer_id,
            bid_idpk=bid_idpk, now=now,
            proposal_expires_at=now - timedelta(seconds=1),
        )
        wallet = FakeWallet()

        stats = asyncio.run(expire_stale_swaps(
            session, now=now, client=wallet,
        ))
        session.commit()

        assert stats["expired_swaps"] == 1
        assert stats["released_holds"] == 1
        assert wallet.get_calls == []
        record = session.exec(
            select(SwapRecord).where(SwapRecord.idpk == bid_idpk)
        ).one()
        assert record.status == "EXPIRED"
        assert session.exec(
            select(MarketBid).where(MarketBid.idpk == bid_idpk)
        ).one().status == "EXPIRED"
        assert session.exec(
            select(OfferReservation).where(
                OfferReservation.bid_idpk == bid_idpk
            )
        ).one().status == "RELEASED"


def test_wallet_evidence_expires_and_pending_bid_after_last_bid():
    now = datetime.now(timezone.utc)
    cycle_id = f"e2055-{uuid4()}"
    offer_id = f"KLD-{cycle_id}"
    expired_idpk = str(uuid4())
    alive_idpk = str(uuid4())
    swap_expired = f"swp-{uuid4().hex}"
    swap_alive = f"swp-{uuid4().hex}"

    with Session(engine) as session:
        _context(
            session, cycle_id=cycle_id, offer_id=offer_id, now=now,
            last_bid_at=now - timedelta(seconds=60),
        )
        _proposer(
            session, cycle_id=cycle_id, offer_id=offer_id,
            bid_idpk=expired_idpk, now=now,
            swap_id=swap_expired,
            status="PROPOSED",
            proposal_http_accepted_at=now - timedelta(seconds=60),
            proposal_expires_at=now - timedelta(seconds=1),
            expires_at=now - timedelta(seconds=1),
        )
        _proposer(
            session, cycle_id=cycle_id, offer_id=offer_id,
            bid_idpk=alive_idpk, now=now,
            swap_id=swap_alive,
            status="PROPOSED",
            proposal_http_accepted_at=now - timedelta(seconds=60),
            proposal_expires_at=now - timedelta(seconds=1),
            expires_at=now - timedelta(seconds=1),
        )
        pending = MarketBid(
            idpk=str(uuid4()),
            msg_id=str(uuid4()),
            cycle_id=cycle_id,
            direction="OUTGOING",
            offer_id=offer_id,
            counterpart_city_id="MET",
            quantity=50,
            status="PENDING",
            created_at=now - timedelta(seconds=90),
        )
        session.add(pending)
        session.commit()

        wallet = FakeWallet({
            swap_expired: "expired",
            swap_alive: "proposed",
        })

        stats = asyncio.run(expire_stale_swaps(
            session, now=now, client=wallet,
        ))
        session.commit()

        assert stats["wallet_checked"] == 2
        assert stats["expired_swaps"] == 1
        assert sorted(wallet.get_calls) == sorted([swap_expired, swap_alive])
        assert sorted(wallet.cancel_calls) == [swap_alive]

        expired = session.exec(
            select(SwapRecord).where(SwapRecord.idpk == expired_idpk)
        ).one()
        alive = session.exec(
            select(SwapRecord).where(SwapRecord.idpk == alive_idpk)
        ).one()
        assert expired.status == "EXPIRED"
        assert alive.status == "PROPOSED"
        assert session.exec(
            select(OfferReservation).where(
                OfferReservation.bid_idpk == expired_idpk
            )
        ).one().status == "RELEASED"
        assert session.exec(
            select(OfferReservation).where(
                OfferReservation.bid_idpk == alive_idpk
            )
        ).one().status == "HOLD"
        assert session.exec(
            select(MarketBid).where(MarketBid.idpk == pending.idpk)
        ).one().status == "EXPIRED"
