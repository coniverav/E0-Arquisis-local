"""E2-034 / RF03: rechazo comercial, available exacto y serialización AD4.

Ejecutar SOLO en una base energyshark_test_*; no requiere central ni RabbitMQ.
Todos los mensajes ingresan por /internal/messages (excepto tests de servicio).
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import Barrier
from time import sleep
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlmodel import Session, select

from app.config import CITY_ID
from app.database import engine, run_migrations
from app.main import app
from app.models import (
    Cycle, InboundMessage, MarketBid, MarketCycle, MarketOffer,
    OfferReservation, OutboundMessage, ProcessedIdpk, SwapRecord,
)
from app.services.seller_bid_acceptance import reserve_incoming_bid
from app.services.seller_bid_rejections import (
    REJECTION_CODES, available_kwh, enqueue_bid_rejected,
)


def setup_module():
    assert (engine.url.database or "").startswith("energyshark_test_")
    run_migrations()


@pytest.fixture
def scenario():
    now = datetime.now(timezone.utc)
    cid = f"e2034-{uuid4()}"
    oid = f"{CITY_ID}-{cid}"
    with Session(engine) as session:
        session.add(Cycle(
            cycle_id=cid, status_idpk=str(uuid4()), created_at=now,
            market_opens_at=now - timedelta(minutes=2),
            market_last_bid_at=now + timedelta(minutes=3),
            market_last_offer_at=now + timedelta(minutes=2),
            market_closes_at=now + timedelta(minutes=5),
            valid_until=now + timedelta(minutes=6),
            status_payload={"market": {"minBid": 5}},
        ))
        session.add(MarketOffer(
            offer_id=oid, cycle_id=cid, city_id=CITY_ID, is_local=True,
            initial_quantity=100, remaining_quantity=100,
            price_per_energy=Decimal("4.25"), status="ACTIVE",
            published_at=now, last_updated_at=now,
        ))
        session.commit()
    yield {"cycle": cid, "offer": oid, "now": now}
    # Limpieza de este escenario por si el usuario no usa conftest global.
    with Session(engine) as session:
        keys = session.exec(select(InboundMessage.idpk).where(
            InboundMessage.cycle_id == cid,
            InboundMessage.idpk.is_not(None),
        )).all()
        if keys:
            session.exec(delete(ProcessedIdpk).where(ProcessedIdpk.idpk.in_(keys)))
        session.exec(delete(OutboundMessage).where(OutboundMessage.cycle_id == cid))
        session.exec(delete(InboundMessage).where(InboundMessage.cycle_id == cid))
        session.exec(delete(SwapRecord).where(SwapRecord.cycle_id == cid))
        session.exec(delete(OfferReservation).where(OfferReservation.offer_id == oid))
        session.exec(delete(MarketBid).where(MarketBid.cycle_id == cid))
        session.exec(delete(MarketOffer).where(MarketOffer.offer_id == oid))
        session.exec(delete(MarketCycle).where(MarketCycle.cycle_id == cid))
        session.exec(delete(Cycle).where(Cycle.cycle_id == cid))
        session.commit()


def bid_message(s, *, city="COR", quantity=80, key=None, target=None):
    return {
        "idpk": str(key or uuid4()), "msgId": str(target or uuid4()),
        "type": "bid", "timestamp": datetime.now(timezone.utc).isoformat(),
        "cityId": city, "cycleId": s["cycle"],
        "data": {"offerId": s["offer"], "quantity": quantity},
    }


def post(payload):
    response = TestClient(app).post("/internal/messages", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def rejections(s):
    with Session(engine) as session:
        return session.exec(select(OutboundMessage).where(
            OutboundMessage.cycle_id == s["cycle"],
            OutboundMessage.message_type == "bid-rejected",
        )).all()


def assert_rejection(s, bid, reason, available=None):
    sent = [msg for msg in rejections(s) if msg.target_msg_id == bid["msgId"]]
    assert len(sent) == 1
    record = sent[0]
    msg = record.payload
    assert record.status == "PENDING" and record.dispatch_required is True
    assert record.exchange_name == "energy.x"
    assert record.routing_key == f'city.{bid["cityId"]}'
    assert msg["cityId"] == CITY_ID and msg["cycleId"] == s["cycle"]
    assert msg["type"] == "bid-rejected"
    assert msg["msgId"] == record.msg_id and msg["idpk"] == record.idpk
    assert msg["msgId"] != msg["idpk"]
    assert msg["reason"] == reason and msg["code"] == REJECTION_CODES[reason]
    assert msg["data"]["target"] == bid["msgId"]
    assert msg["data"]["offerId"] == s["offer"]
    assert msg["data"]["requested"] == bid["data"]["quantity"]
    if reason == "QUANTITY_UNAVAILABLE":
        assert type(msg["data"]["available"]) is int
        assert msg["data"]["available"] == available
    else:
        assert "available" not in msg["data"]
    with Session(engine) as session:
        stored = session.exec(select(MarketBid).where(
            MarketBid.idpk == bid["idpk"])).one()
        assert stored.status == "REJECTED"
        assert stored.rejection_reason == reason
        assert stored.available_on_rejection == available
    return msg


@pytest.mark.parametrize("reason,quantity,city,change", [
    ("QUANTITY_UNAVAILABLE", 101, "COR", "none"),
    ("BID_TOO_SMALL", 4, "COR", "none"),
    ("OFFER_WITHDRAWN", 20, "COR", "withdraw"),
    ("MARKET_CLOSED", 20, "COR", "closed"),
    ("SELF_TRADE", 20, CITY_ID, "none"),
])
def test_los_cinco_motivos_contractuales(scenario, reason, quantity, city, change):
    if change != "none":
        with Session(engine) as session:
            if change == "withdraw":
                offer = session.exec(select(MarketOffer).where(
                    MarketOffer.offer_id == scenario["offer"])).one()
                offer.status = "WITHDRAWN"
            else:
                cycle = session.get(Cycle, scenario["cycle"])
                cycle.market_last_bid_at = scenario["now"] - timedelta(seconds=1)
            session.commit()
    bid = bid_message(scenario, quantity=quantity, city=city)
    assert post(bid)["status"] == "ok"
    assert_rejection(scenario, bid, reason, available=100 if reason == "QUANTITY_UNAVAILABLE" else None)
    with Session(engine) as session:
        assert not session.exec(select(SwapRecord).where(
            SwapRecord.cycle_id == scenario["cycle"])).all()
        assert not session.exec(select(OfferReservation).where(
            OfferReservation.offer_id == scenario["offer"])).all()


def test_available_exactamente_remaining_menos_hold(scenario):
    with Session(engine) as session:
        for status, count in (("HOLD", 18), ("HOLD", 27),
                              ("RELEASED", 25), ("COMMITTED", 12)):
            session.add(OfferReservation(
                offer_id=scenario["offer"], bid_idpk=str(uuid4()),
                quantity=count, status=status, created_at=scenario["now"],
            ))
        session.commit()
        offer = session.exec(select(MarketOffer).where(
            MarketOffer.offer_id == scenario["offer"]
        ).with_for_update()).one()
        assert available_kwh(session, offer) == 55
        session.commit()
    bid = bid_message(scenario, quantity=56)
    assert post(bid)["status"] == "ok"
    assert_rejection(scenario, bid, "QUANTITY_UNAVAILABLE", available=55)
    with Session(engine) as session:
        holds = session.exec(select(OfferReservation).where(
            OfferReservation.offer_id == scenario["offer"],
            OfferReservation.status == "HOLD",
        )).all()
        assert len(holds) == 2  # un rechazo no altera reservas previas


def test_available_cero_cuando_todo_esta_en_hold(scenario):
    with Session(engine) as session:
        session.add(OfferReservation(
            offer_id=scenario["offer"], bid_idpk=str(uuid4()),
            quantity=100, status="HOLD", created_at=scenario["now"],
        ))
        session.commit()
    bid = bid_message(scenario, quantity=20)
    assert post(bid)["status"] == "ok"
    assert_rejection(scenario, bid, "QUANTITY_UNAVAILABLE", available=0)


def test_ultima_verificacion_no_reserva_despues_lastbidat(scenario):
    """Un bid validado antes del deadline NO puede reservarse después."""
    key = str(uuid4())
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        session.add(MarketBid(
            idpk=key, msg_id=str(uuid4()), cycle_id=scenario["cycle"],
            offer_id=scenario["offer"], counterpart_city_id="COR",
            direction="INCOMING", quantity=20, status="PENDING",
            created_at=now,
        ))
        session.commit()
        cycle = session.get(Cycle, scenario["cycle"])
        # Igualdad exacta con lastBidAt: ya no se pueden aceptar bids.
        result = reserve_incoming_bid(session, bid_idpk=key, now=cycle.market_last_bid_at)
        assert result["status"] == "REJECTED"
        assert result["reason"] == "MARKET_CLOSED"
        session.commit()
    with Session(engine) as session:
        assert not session.exec(select(OfferReservation).where(
            OfferReservation.bid_idpk == key)).all()
        assert not session.exec(select(SwapRecord).where(
            SwapRecord.idpk == key)).all()
        msg = session.exec(select(OutboundMessage).where(
            OutboundMessage.target_msg_id == session.exec(select(MarketBid.msg_id).where(
                MarketBid.idpk == key)).one(),
            OutboundMessage.message_type == "bid-rejected",
        )).one()
        assert msg.payload["reason"] == "MARKET_CLOSED"
        assert "available" not in msg.payload["data"]


def test_revalidacion_luego_de_retiro_de_oferta(scenario):
    key = str(uuid4())
    with Session(engine) as session:
        session.add(MarketBid(
            idpk=key, msg_id=str(uuid4()), cycle_id=scenario["cycle"],
            offer_id=scenario["offer"], counterpart_city_id="COR",
            direction="INCOMING", quantity=20, status="PENDING",
            created_at=scenario["now"],
        ))
        session.commit()
        offer = session.exec(select(MarketOffer).where(
            MarketOffer.offer_id == scenario["offer"])).one()
        offer.status = "WITHDRAWN"
        session.commit()
        result = reserve_incoming_bid(session, bid_idpk=key)
        assert result["reason"] == "OFFER_WITHDRAWN"
        session.commit()
    with Session(engine) as session:
        assert not session.exec(select(OfferReservation).where(
            OfferReservation.offer_id == scenario["offer"])).all()
        assert not session.exec(select(SwapRecord).where(
            SwapRecord.idpk == key)).all()


def test_rechazo_idempotente_sin_doblar_outbox(scenario):
    bid = bid_message(scenario, quantity=4)
    assert post(bid)["status"] == "ok"
    repeat = dict(bid, msgId=str(uuid4()),
                  timestamp=datetime.now(timezone.utc).isoformat())
    assert post(repeat)["status"] == "duplicate"
    assert len(rejections(scenario)) == 1
    with Session(engine) as session:
        results = session.exec(select(ProcessedIdpk).where(
            ProcessedIdpk.idpk == bid["idpk"])).all()
        assert len(results) == 1


def test_concurrencia_en_ruta_y_available_real(scenario):
    """40 ms entre envíos NO garantizan el orden de adquisición del lock."""
    barrier = Barrier(2)
    first = bid_message(scenario, city="COR", quantity=80)
    second = bid_message(scenario, city="HGW", quantity=80)

    def send(msg, delay):
        barrier.wait(timeout=15)
        sleep(delay)
        return post(msg)

    with ThreadPoolExecutor(max_workers=2) as pool:
        fa = pool.submit(send, first, 0.0)
        fb = pool.submit(send, second, 0.040)
        assert fa.result(timeout=30)["status"] == "ok"
        assert fb.result(timeout=30)["status"] == "ok"
    with Session(engine) as session:
        bids = session.exec(select(MarketBid).where(
            MarketBid.cycle_id == scenario["cycle"],
            MarketBid.direction == "INCOMING",
        )).all()
        holds = session.exec(select(OfferReservation).where(
            OfferReservation.offer_id == scenario["offer"],
            OfferReservation.status == "HOLD",
        )).all()
        swaps = session.exec(select(SwapRecord).where(
            SwapRecord.cycle_id == scenario["cycle"],
            SwapRecord.role == "PROPOSER",
        )).all()
    assert len(holds) == len(swaps) == 1
    assert holds[0].quantity == 80
    assert len([b for b in bids if b.status == "RESERVED"]) == 1
    denied = next(b for b in bids if b.status == "REJECTED")
    response = [msg for msg in rejections(scenario) if msg.target_msg_id == denied.msg_id]
    assert len(response) == 1
    assert response[0].routing_key == f"city.{denied.counterpart_city_id}"
    assert response[0].payload["reason"] == "QUANTITY_UNAVAILABLE"
    assert response[0].payload["data"]["available"] == 20
    assert response[0].payload["data"]["target"] == denied.msg_id


@pytest.mark.parametrize("available", [None, -1, 1.0, True, "0", 20])
def test_helper_no_inventa_available_ni_admite_valores_incorrectos(scenario, available):
    event = bid_message(scenario, quantity=20)
    env = SimpleNamespace(msgId=event["msgId"], cycleId=event["cycleId"], data=event["data"])
    with Session(engine) as session:
        with pytest.raises(ValueError):
            enqueue_bid_rejected(
                session, payload=env, buyer="COR", reason="QUANTITY_UNAVAILABLE",
                now=scenario["now"], available=available,
            )
        session.rollback()


def test_rechazo_en_misma_transaccion_puede_revertirse(scenario):
    event = bid_message(scenario, quantity=101)
    env = SimpleNamespace(msgId=event["msgId"], cycleId=event["cycleId"], data=event["data"])
    with Session(engine) as session:
        enqueue_bid_rejected(session, payload=env, buyer="COR",
                             reason="QUANTITY_UNAVAILABLE", available=100,
                             now=scenario["now"])
        session.rollback()
    with Session(engine) as session:
        assert not session.exec(select(OutboundMessage).where(
            OutboundMessage.target_msg_id == event["msgId"])).all()


def test_helper_no_duplica_rechazo_equivalente_y_detecta_conflictos(scenario):
    event = bid_message(scenario, quantity=101)
    env = SimpleNamespace(msgId=event["msgId"], cycleId=event["cycleId"], data=event["data"])
    with Session(engine) as session:
        first = enqueue_bid_rejected(session, payload=env, buyer="COR",
                                      reason="QUANTITY_UNAVAILABLE", available=100,
                                      now=scenario["now"])
        second = enqueue_bid_rejected(session, payload=env, buyer="COR",
                                       reason="QUANTITY_UNAVAILABLE", available=100,
                                       now=scenario["now"])
        assert first.id == second.id
        with pytest.raises(RuntimeError, match="CONFLICTING_BID_REJECTION"):
            enqueue_bid_rejected(session, payload=env, buyer="HGW",
                                 reason="QUANTITY_UNAVAILABLE", available=100,
                                 now=scenario["now"])
        session.rollback()
