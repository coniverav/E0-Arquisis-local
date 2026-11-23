"""E3-038: la reserva seller y StartExecution comparten correlación durable.

Requiere DATABASE_URL=.../energyshark_test_* (fixture de master/tests).
La ejecución AWS se simula como intención outbox: NO invoca AWS ni Wallet.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from uuid import uuid4

import pytest
from sqlmodel import Session, select

from app.config import CITY_ID
from app.database import engine
from app.models import (
    Cycle, MarketOffer, MarketBid, OfferReservation, OutboundMessage,
    SagaExecution, SwapRecord,
)
from app.services.seller_bid_acceptance import reserve_incoming_bid

ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:energyshark-seller-e3"


@pytest.fixture
def seller_scenario(monkeypatch):
    monkeypatch.setenv("E3_SELLER_SAGA_ENABLED", "true")
    monkeypatch.setenv("E3_SELLER_STATE_MACHINE_ARN", ARN)
    now = datetime.now(timezone.utc)
    cycle_id = "e3038-" + str(uuid4())
    offer_id = f"{CITY_ID}-{cycle_id}"
    bid_idpk = str(uuid4())
    with Session(engine) as session:
        session.add(Cycle(
            cycle_id=cycle_id, created_at=now, status_idpk=str(uuid4()),
            market_opens_at=now - timedelta(minutes=1),
            market_last_offer_at=now + timedelta(minutes=4),
            market_last_bid_at=now + timedelta(minutes=3),
            market_closes_at=now + timedelta(minutes=5),
            valid_until=now + timedelta(minutes=6),
            status_payload={"market": {"minBid": 5}},
        ))
        session.add(MarketOffer(
            offer_id=offer_id, cycle_id=cycle_id, city_id=CITY_ID,
            is_local=True, initial_quantity=100, remaining_quantity=100,
            price_per_energy=Decimal("3.50"), status="ACTIVE",
            published_at=now, last_updated_at=now,
        ))
        session.add(MarketBid(
            idpk=bid_idpk, msg_id=str(uuid4()), cycle_id=cycle_id,
            direction="INCOMING", offer_id=offer_id,
            counterpart_city_id="COR", quantity=40,
            status="PENDING", created_at=now,
        ))
        session.commit()
    return now, cycle_id, offer_id, bid_idpk


def test_seller_start_has_durable_execution_and_identical_outbox_id(seller_scenario):
    now, cycle_id, offer_id, bid_idpk = seller_scenario
    with Session(engine) as session:
        assert reserve_incoming_bid(session, bid_idpk=bid_idpk, now=now)["status"] == "RESERVED"
        session.commit()
    with Session(engine) as session:
        saga = session.exec(select(SagaExecution).where(SagaExecution.idpk == bid_idpk)).one()
        outbox = session.exec(select(OutboundMessage).where(
            OutboundMessage.idpk == bid_idpk,
            OutboundMessage.delivery_kind == "STEP_FUNCTIONS",
        )).one()
        hold = session.exec(select(OfferReservation).where(
            OfferReservation.bid_idpk == bid_idpk,
        )).one()
        swap = session.exec(select(SwapRecord).where(SwapRecord.idpk == bid_idpk)).one()
        payload = json.loads(outbox.execution_input)
        assert saga.role == "SELLER" and saga.status == "PENDING_START"
        assert saga.cycle_id == cycle_id
        assert saga.timeout_at == now + timedelta(minutes=6)
        assert hold.status == "HOLD" and hold.quantity == 40
        assert swap.status == "SUBMITTED" and swap.idempotency_key == bid_idpk
        assert outbox.execution_name == payload["sagaExecutionId"] == bid_idpk
        assert payload["offerId"] == offer_id
        assert payload["quantity"] == 40
        assert payload["timeouts"] == {"proposalSeconds": 30, "terminalSeconds": 30}
        assert payload["timeoutAt"] == saga.timeout_at.isoformat().replace("+00:00", "Z")
        assert outbox.expires_at is None  # recuperación tardía todavía necesaria

    with Session(engine) as session:
        again = reserve_incoming_bid(session, bid_idpk=bid_idpk, now=now)
        assert again["duplicate"] is True
        session.commit()
    with Session(engine) as session:
        assert len(session.exec(select(SagaExecution)).all()) == 1
        assert len(session.exec(select(OutboundMessage).where(
            OutboundMessage.delivery_kind == "STEP_FUNCTIONS",
        )).all()) == 1


def test_seller_start_rollback_removes_saga_and_hold_together(seller_scenario):
    now, cycle_id, offer_id, bid_idpk = seller_scenario
    with Session(engine) as session:
        assert reserve_incoming_bid(session, bid_idpk=bid_idpk, now=now)["status"] == "RESERVED"
        session.rollback()
    with Session(engine) as session:
        assert not session.exec(select(SagaExecution)).all()
        assert not session.exec(select(OfferReservation)).all()
        assert not session.exec(select(SwapRecord)).all()
        assert not session.exec(select(OutboundMessage).where(
            OutboundMessage.delivery_kind == "STEP_FUNCTIONS"
        )).all()
        bid = session.exec(select(MarketBid).where(MarketBid.idpk == bid_idpk)).one()
        assert bid.status == "PENDING"


def test_insufficient_energy_rejects_without_saga(seller_scenario):
    now, cycle_id, offer_id, bid_idpk = seller_scenario
    with Session(engine) as session:
        bid = session.exec(select(MarketBid).where(MarketBid.idpk == bid_idpk)).one()
        bid.quantity = 101
        session.add(bid)
        session.commit()
    with Session(engine) as session:
        result = reserve_incoming_bid(session, bid_idpk=bid_idpk, now=now)
        assert result["status"] == "REJECTED"
        assert result["reason"] == "QUANTITY_UNAVAILABLE"
        session.commit()
    with Session(engine) as session:
        assert not session.exec(select(SagaExecution)).all()
        assert not session.exec(select(OfferReservation)).all()
        assert not session.exec(select(SwapRecord)).all()
