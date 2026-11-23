"""E3-041: Wallet swap-proposed y bid-accepted outbox, PostgreSQL de test.

Sin RabbitMQ, AWS ni HTTP. Usa el mismo servicio E2 del ingreso Wallet.
"""
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from sqlmodel import Session, select

from app.config import CITY_ID
from app.database import engine
from app.models import (Cycle, MarketOffer, MarketBid, OfferReservation,
                        OutboundMessage, SwapRecord)
from app.services.seller_bid_acceptance import reserve_incoming_bid
from app.services.seller_bid_accepted import enqueue_bid_accepted_after_swap_proposed
from app.services.wallet_service import WalletService

LAMBDA_DIR = Path(__file__).resolve().parents[2] / "infra" / "cdk" / "lambda_src" / "saga_task"
sys.path.insert(0, str(LAMBDA_DIR))
from postgres_store import PostgresStore  # noqa: E402
from seller_bid_accepted import (  # noqa: E402
    verify_persisted_bid_accepted, SellerBidAcceptedPending,
    SellerBidAcceptedInconsistent,
)

ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:e3041"


@pytest.fixture
def seller_case(monkeypatch):
    monkeypatch.setenv("E3_SELLER_SAGA_ENABLED", "true")
    monkeypatch.setenv("E3_SELLER_STATE_MACHINE_ARN", ARN)
    now = datetime.now(timezone.utc)
    cycle = "e3041-" + str(uuid4())
    offer = f"{CITY_ID}-{cycle}"
    bid = str(uuid4())
    with Session(engine) as session:
        session.add(Cycle(
            cycle_id=cycle, status_idpk=str(uuid4()), created_at=now,
            market_opens_at=now - timedelta(minutes=1),
            market_last_offer_at=now + timedelta(minutes=4),
            market_last_bid_at=now + timedelta(minutes=3),
            market_closes_at=now + timedelta(minutes=5),
            valid_until=now + timedelta(minutes=6),
            status_payload={"market": {"minBid": 5}},
        ))
        session.add(MarketOffer(
            offer_id=offer, cycle_id=cycle, city_id=CITY_ID,
            is_local=True, initial_quantity=100, remaining_quantity=100,
            price_per_energy=Decimal("4.25"), status="ACTIVE",
            published_at=now, last_updated_at=now,
        ))
        session.add(MarketBid(
            idpk=bid, msg_id=str(uuid4()), cycle_id=cycle,
            direction="INCOMING", offer_id=offer,
            counterpart_city_id="COR", quantity=40,
            status="PENDING", created_at=now,
        ))
        session.commit()
    with Session(engine) as session:
        assert reserve_incoming_bid(session, bid_idpk=bid, now=now)["status"] == "RESERVED"
        session.commit()
    store = PostgresStore(
        engine.url.render_as_string(hide_password=False).replace(
            "postgresql+psycopg://", "postgresql://", 1),
        city_id=CITY_ID, now=lambda: now,
    )
    return now, cycle, offer, bid, store


def _accepted_kwargs(case, *, swap_id, expires_at):
    _, cycle, _, bid, _ = case
    return dict(bid_idpk=bid, cycle_id=cycle, swap_id=swap_id,
                expires_at=expires_at.isoformat().replace("+00:00", "Z"),
                saga_execution_id=bid)


def _wallet_event(case):
    now, cycle, offer, bid, _ = case
    swap_id = "swp-" + uuid4().hex
    expires_at = now + timedelta(seconds=45)
    event_data = {
        "idpk": bid, "swapId": swap_id, "proposer": CITY_ID,
        "give": {"asset": "KWH", "amount": 40},
        "take": {"asset": "CR", "amount": 170.0},
        "memo": offer,
        "expiresAt": expires_at.isoformat().replace("+00:00", "Z"),
    }
    claims = {
        "iss": "wallet.energyshark", "sub": swap_id,
        "iat": int(now.timestamp()), "exp": int(expires_at.timestamp()),
        "proposer": CITY_ID,
        "give": event_data["give"], "take": event_data["take"],
        "memo": offer,
    }
    event_data["token"] = jwt.encode(
        claims, ec.generate_private_key(ec.SECP256R1()), algorithm="ES256",
    )
    return event_data, expires_at


def _outbox(session):
    return session.exec(select(OutboundMessage).where(
        OutboundMessage.message_type == "bid-accepted",
    )).all()


def test_http_202_without_swap_event_cannot_verify_bid_accepted(seller_case):
    now, cycle, offer, bid, store = seller_case
    with pytest.raises(SellerBidAcceptedPending):
        verify_persisted_bid_accepted(
            store, **_accepted_kwargs(seller_case, swap_id="http-only",
                                      expires_at=now + timedelta(seconds=45)),
        )
    with Session(engine) as session:
        assert len(_outbox(session)) == 0
        assert session.exec(select(OfferReservation)).one().status == "HOLD"


def test_official_event_creates_one_outbox_and_lambda_only_verifies(seller_case):
    now, cycle, offer, bid, store = seller_case
    data, expires_at = _wallet_event(seller_case)
    with Session(engine) as session:
        record = WalletService(client=object()).process_swap_event(
            session, event_type="swap-proposed", data=data,
        )
        assert record is not None
        assert enqueue_bid_accepted_after_swap_proposed(
            session, record=record, event_data=data, sender="central", now=now,
        )
        session.commit()
    args = _accepted_kwargs(seller_case, swap_id=data["swapId"],
                            expires_at=expires_at)
    first = verify_persisted_bid_accepted(store, **args)
    assert first["status"] == "QUEUED" and first["alreadyEnqueued"] is True
    assert verify_persisted_bid_accepted(store, **args) == first
    with Session(engine) as session:
        messages = _outbox(session)
        assert len(messages) == 1
        assert messages[0].payload["data"]["swapToken"] == data["token"]
        assert messages[0].routing_key == "city.COR"
        assert session.exec(select(OfferReservation)).one().status == "HOLD"
        assert session.exec(select(MarketBid)).one().status == "ACCEPTED"
        record = session.exec(select(SwapRecord)).one()
        assert not enqueue_bid_accepted_after_swap_proposed(
            session, record=record, event_data=data, sender="central", now=now,
        )
        session.commit()
    with Session(engine) as session:
        assert len(_outbox(session)) == 1


def test_wrong_swap_or_expiry_cannot_forge_acceptance(seller_case):
    _, _, _, _, store = seller_case
    data, expires = _wallet_event(seller_case)
    with Session(engine) as session:
        record = WalletService(client=object()).process_swap_event(
            session, event_type="swap-proposed", data=data,
        )
        assert enqueue_bid_accepted_after_swap_proposed(
            session, record=record, event_data=data,
            sender="central", now=store.now(),
        )
        session.commit()
    with pytest.raises(SellerBidAcceptedInconsistent):
        verify_persisted_bid_accepted(store, **_accepted_kwargs(
            seller_case, swap_id="forged", expires_at=expires))
    with pytest.raises(SellerBidAcceptedInconsistent):
        verify_persisted_bid_accepted(store, **_accepted_kwargs(
            seller_case, swap_id=data["swapId"],
            expires_at=expires + timedelta(seconds=10)))
    with Session(engine) as session:
        assert len(_outbox(session)) == 1


def test_central_event_rollback_keeps_bid_reserved_and_no_outbox(seller_case):
    now, _, _, _, store = seller_case
    data, expires_at = _wallet_event(seller_case)
    with Session(engine) as session:
        record = WalletService(client=object()).process_swap_event(
            session, event_type="swap-proposed", data=data,
        )
        assert enqueue_bid_accepted_after_swap_proposed(
            session, record=record, event_data=data,
            sender="central", now=now,
        )
        session.rollback()
    with pytest.raises(SellerBidAcceptedPending):
        verify_persisted_bid_accepted(
            store, **_accepted_kwargs(seller_case, swap_id=data["swapId"],
                                      expires_at=expires_at),
        )
    with Session(engine) as session:
        assert not _outbox(session)
        assert session.exec(select(MarketBid)).one().status == "RESERVED"
        assert session.exec(select(OfferReservation)).one().status == "HOLD"


def test_noncentral_event_never_creates_bid_accepted(seller_case):
    now, _, _, _, store = seller_case
    data, expires_at = _wallet_event(seller_case)
    with Session(engine) as session:
        record = WalletService(client=object()).process_swap_event(
            session, event_type="swap-proposed", data=data,
        )
        assert not enqueue_bid_accepted_after_swap_proposed(
            session, record=record, event_data=data,
            sender="untrusted", now=now,
        )
        session.commit()
    with pytest.raises(SellerBidAcceptedPending):
        verify_persisted_bid_accepted(
            store, **_accepted_kwargs(seller_case, swap_id=data["swapId"],
                                      expires_at=expires_at),
        )
    with Session(engine) as session:
        assert not _outbox(session)
        assert session.exec(select(MarketBid)).one().status == "RESERVED"
