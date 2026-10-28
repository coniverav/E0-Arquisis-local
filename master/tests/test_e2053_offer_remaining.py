"""E2-053: commit/release de remaining y offer-updated idempotentes."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from sqlmodel import Session, select

from app.database import engine
from app.models import (
    Cycle,
    MarketOffer,
    OfferReservation,
    OutboundMessage,
)
from app.services.wallet_service import WalletService


def _context(session, *, cycle_id, offer_id, now):
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
        market_last_offer_at=now + timedelta(minutes=5),
        market_closes_at=now + timedelta(minutes=6),
        valid_until=now + timedelta(minutes=7),
    ))
    session.add(MarketOffer(
        offer_id=offer_id,
        cycle_id=cycle_id,
        city_id="KLD",
        is_local=True,
        initial_quantity=100,
        remaining_quantity=100,
        price_per_energy=Decimal("10"),
        status="ACTIVE",
        published_at=now,
        last_updated_at=now,
    ))
    session.commit()


def _reservation(session, *, offer_id, bid_idpk, quantity, now):
    reservation = OfferReservation(
        offer_id=offer_id,
        bid_idpk=bid_idpk,
        quantity=quantity,
        status="HOLD",
        created_at=now,
    )
    session.add(reservation)
    session.commit()
    return reservation


def test_commit_reduces_once_and_release_does_not_consume_remaining():
    now = datetime.now(timezone.utc)
    cycle_id = f"e2053-{uuid4()}"
    offer_id = f"KLD-{cycle_id}"
    service = WalletService()

    with Session(engine) as session:
        _context(session, cycle_id=cycle_id, offer_id=offer_id, now=now)
        _reservation(
            session, offer_id=offer_id, bid_idpk="bid-commit",
            quantity=40, now=now,
        )

        service._update_offer_and_reservation(
            session, offer_id=offer_id, bid_idpk="bid-commit", action="COMMIT",
        )
        service._update_offer_and_reservation(
            session, offer_id=offer_id, bid_idpk="bid-commit", action="COMMIT",
        )
        session.commit()

        offer = session.exec(
            select(MarketOffer).where(MarketOffer.offer_id == offer_id)
        ).one()
        assert offer.remaining_quantity == 60
        updates = session.exec(
            select(OutboundMessage).where(
                OutboundMessage.cycle_id == cycle_id,
                OutboundMessage.message_type == "offer-updated",
            )
        ).all()
        assert len(updates) == 1
        assert updates[0].payload["data"]["remaining"] == 60

        _reservation(
            session, offer_id=offer_id, bid_idpk="bid-release",
            quantity=20, now=now,
        )
        service._update_offer_and_reservation(
            session, offer_id=offer_id, bid_idpk="bid-release", action="RELEASE",
        )
        session.commit()

        offer = session.exec(
            select(MarketOffer).where(MarketOffer.offer_id == offer_id)
        ).one()
        released = session.exec(
            select(OfferReservation).where(
                OfferReservation.bid_idpk == "bid-release"
            )
        ).one()
        assert offer.remaining_quantity == 60
        assert released.status == "RELEASED"
        assert released.released_at is not None
