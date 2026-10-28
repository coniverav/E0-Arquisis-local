"""E2-030: recepción de bid, correlación, reglas de negocio y outbox.

Sólo ejecutar contra una base de datos de pruebas.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.config import CITY_ID
from app.database import engine, run_migrations
from app.main import app
from app.models import (
    Cycle, InboundMessage, MarketBid, MarketCycle, MarketOffer,
    OfferReservation, OutboundMessage,
    ProcessedIdpk, SwapRecord,
)


def setup_module():
    run_migrations()


@pytest.fixture
def scenario():
    now = datetime.now(timezone.utc)
    cid = f"e2030-{uuid4()}"
    oid = f"{CITY_ID}-{cid}"
    with Session(engine) as session:
        session.add(Cycle(
            cycle_id=cid, created_at=now, status_idpk=str(uuid4()),
            market_opens_at=now - timedelta(minutes=1),
            market_last_bid_at=now + timedelta(minutes=3),
            market_last_offer_at=now + timedelta(minutes=2),
            market_closes_at=now + timedelta(minutes=5),
            valid_until=now + timedelta(minutes=6),
            status_payload={"market": {"minBid": 5}},
        ))
        session.add(MarketOffer(
            offer_id=oid, cycle_id=cid, city_id=CITY_ID, is_local=True,
            initial_quantity=20, remaining_quantity=20,
            price_per_energy=Decimal("10.00"), status="ACTIVE",
            published_at=now, last_updated_at=now,
        ))
        session.commit()
    return {"cycle": cid, "offer": oid, "now": now}


def message(s, *, city="COR", offer=None, cycle=None, quantity=6,
            key=None, msg=None):
    return {
        "idpk": str(key or uuid4()), "msgId": str(msg or uuid4()),
        "type": "bid", "timestamp": datetime.now(timezone.utc).isoformat(),
        "cityId": city, "cycleId": cycle or s["cycle"],
        "data": {"offerId": offer if offer is not None else s["offer"],
                 "quantity": quantity},
    }


def post(body):
    res = TestClient(app).post("/internal/messages", json=body)
    assert res.status_code == 200, res.text
    return res


def db_bid(key):
    with Session(engine) as session:
        return session.exec(select(MarketBid).where(MarketBid.idpk == str(key))).first()


def outbox(key):
    with Session(engine) as session:
        return session.exec(select(OutboundMessage).where(
            OutboundMessage.target_msg_id == str(key),
            OutboundMessage.message_type == "bid-rejected",
        )).all()


def assert_rejection(body, reason, *, available=None):
    result = post(body)
    assert result.json()["status"] == "ok", result.json()
    sent = outbox(body["msgId"])
    assert len(sent) == 1
    event = sent[0].payload
    assert event["type"] == "bid-rejected"
    assert event["reason"] == reason
    assert event["code"] == {
        "QUANTITY_UNAVAILABLE": 409, "OFFER_WITHDRAWN": 410,
        "BID_TOO_SMALL": 422, "MARKET_CLOSED": 410, "SELF_TRADE": 422,
    }[reason]
    assert event["data"]["target"] == body["msgId"]
    assert event["data"]["offerId"] == body["data"]["offerId"]
    assert event["data"]["requested"] == body["data"]["quantity"]
    if available is not None:
        assert event["data"]["available"] == available
    assert sent[0].exchange_name == "energy.x"
    assert sent[0].routing_key == f'city.{body["cityId"]}'
    assert sent[0].dispatch_required
    assert sent[0].status == "PENDING"
    return event


def test_valid_bid_is_pending_and_does_not_reserve_or_start_swap(scenario):
    body = message(scenario)
    res = post(body)
    assert res.json()["status"] == "ok"
    bid = db_bid(body["idpk"])
    assert bid.direction == "INCOMING"
    assert bid.cycle_id == scenario["cycle"]
    assert bid.offer_id == scenario["offer"]
    assert bid.counterpart_city_id == "COR"
    assert bid.quantity == 6 and bid.status == "PENDING"
    with Session(engine) as session:
        offer = session.exec(select(MarketOffer).where(
            MarketOffer.offer_id == scenario["offer"])).one()
        assert offer.remaining_quantity == 20
        assert session.exec(select(OfferReservation).where(
            OfferReservation.offer_id == scenario["offer"])).all() == []
        assert session.exec(select(SwapRecord).where(
            SwapRecord.cycle_id == scenario["cycle"])).all() == []
    assert outbox(body["msgId"]) == []


def test_below_min_bid_rejected_with_422(scenario):
    body = message(scenario, quantity=4)
    assert_rejection(body, "BID_TOO_SMALL")
    bid = db_bid(body["idpk"])
    assert bid is not None and bid.status == "REJECTED"
    assert bid.rejection_reason == "BID_TOO_SMALL"


def test_over_remaining_rejected_with_409_available(scenario):
    body = message(scenario, quantity=21)
    assert_rejection(body, "QUANTITY_UNAVAILABLE", available=20)
    bid = db_bid(body["idpk"])
    assert bid.available_on_rejection == 20


def test_pending_hold_is_not_available_and_is_not_modified(scenario):
    with Session(engine) as session:
        session.add(OfferReservation(
            offer_id=scenario["offer"], bid_idpk=str(uuid4()),
            quantity=8, status="HOLD", created_at=scenario["now"],
        ))
        session.commit()
    body = message(scenario, quantity=15)
    assert_rejection(body, "QUANTITY_UNAVAILABLE", available=12)
    with Session(engine) as session:
        holds = session.exec(select(OfferReservation).where(
            OfferReservation.offer_id == scenario["offer"],
            OfferReservation.status == "HOLD",
        )).all()
        assert len(holds) == 1 and holds[0].quantity == 8


def test_withdrawn_offer_gets_business_rejection(scenario):
    with Session(engine) as session:
        offer = session.exec(select(MarketOffer).where(
            MarketOffer.offer_id == scenario["offer"])).one()
        offer.status = "WITHDRAWN"
        session.commit()
    body = message(scenario)
    assert_rejection(body, "OFFER_WITHDRAWN")
    assert db_bid(body["idpk"]).status == "REJECTED"


def test_unknown_offer_does_not_create_orphan_bid(scenario):
    body = message(scenario, offer=f"{CITY_ID}-unknown-{uuid4()}")
    assert_rejection(body, "OFFER_WITHDRAWN")
    assert db_bid(body["idpk"]) is None
    with Session(engine) as session:
        inbound = session.exec(select(InboundMessage).where(
            InboundMessage.msg_id == body["msgId"])).one()
        assert inbound.offer_id == body["data"]["offerId"]
        assert inbound.status == "PROCESSED"


def test_self_trade_rejected(scenario):
    body = message(scenario, city=CITY_ID)
    assert_rejection(body, "SELF_TRADE")
    assert db_bid(body["idpk"]).rejection_reason == "SELF_TRADE"


def test_before_open_and_after_last_bid_rejected(scenario):
    with Session(engine) as session:
        cycle = session.get(Cycle, scenario["cycle"])
        cycle.market_opens_at = datetime.now(timezone.utc) + timedelta(minutes=1)
        session.commit()
    assert_rejection(message(scenario), "MARKET_CLOSED")
    with Session(engine) as session:
        cycle = session.get(Cycle, scenario["cycle"])
        cycle.market_opens_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        cycle.market_last_bid_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.commit()
    assert_rejection(message(scenario), "MARKET_CLOSED")


def test_after_market_closed_event_rejected(scenario):
    with Session(engine) as session:
        session.add(MarketCycle(cycle_id=scenario["cycle"],
                                closed_at=scenario["now"],
                                closed_msg_id=str(uuid4())))
        session.commit()
    assert_rejection(message(scenario), "MARKET_CLOSED")


def test_same_idpk_with_new_msgid_no_duplicate_bid_or_rejection(scenario):
    key = uuid4()
    first = message(scenario, key=key, quantity=4)
    assert_rejection(first, "BID_TOO_SMALL")
    second = message(scenario, key=key, quantity=4)
    assert post(second).json()["status"] == "duplicate"
    assert outbox(second["msgId"]) == []
    with Session(engine) as session:
        assert len(session.exec(select(MarketBid).where(
            MarketBid.idpk == str(key))).all()) == 1
        assert len(session.exec(select(ProcessedIdpk).where(
            ProcessedIdpk.idpk == str(key))).all()) == 1


def test_idpk_same_but_quantity_changed_is_not_processed(scenario):
    key = uuid4()
    assert post(message(scenario, key=key, quantity=5)).json()["status"] == "ok"
    second = message(scenario, key=key, quantity=8)
    result = post(second)
    assert result.json()["status"] == "not_processed"
    with Session(engine) as session:
        audit = session.exec(select(InboundMessage).where(
            InboundMessage.msg_id == second["msgId"])).one()
        assert audit.status == "NOT_PROCESSED"
        assert audit.reason == "IDPK_PAYLOAD_CONFLICT"
    assert db_bid(key).quantity == 5


def test_other_cycle_offer_mismatch_rejected_and_not_linked(scenario):
    other = f'e2030-other-{uuid4()}'
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        session.add(Cycle(
            cycle_id=other, created_at=now, status_idpk=str(uuid4()),
            market_opens_at=now-timedelta(minutes=1),
            market_last_bid_at=now+timedelta(minutes=2),
            market_closes_at=now+timedelta(minutes=3),
            status_payload={'market': {'minBid': 5}},
        ))
        session.commit()
    body = message(scenario, cycle=other)
    assert_rejection(body, 'OFFER_WITHDRAWN')
    assert db_bid(body['idpk']) is None
