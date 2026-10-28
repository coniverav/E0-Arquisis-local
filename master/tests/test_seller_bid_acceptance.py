"""E2-031/AD4: PostgreSQL real, dos compradores concurrentes y 40 ms."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import Barrier
from time import sleep
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
    WalletBalanceSnapshot,
)
from app.services.seller_bid_acceptance import reserve_incoming_bid
from app.services.wallet_service import WalletService


def setup_module():
    assert (engine.url.database or "").startswith("energyshark_test_")
    run_migrations()


@pytest.fixture
def scenario():
    now = datetime.now(timezone.utc)
    cid = f"e2031-{uuid4()}"
    oid = f"{CITY_ID}-{cid}"
    with Session(engine) as session:
        session.add(Cycle(
            cycle_id=cid, created_at=now, status_idpk=str(uuid4()),
            market_opens_at=now - timedelta(minutes=1),
            market_last_bid_at=now + timedelta(minutes=3),
            market_last_offer_at=now + timedelta(minutes=4),
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
    with Session(engine) as session:
        # Limpieza explícita: esta suite también funciona sin conftest.py global.
        keys = session.exec(select(InboundMessage.idpk).where(
            InboundMessage.cycle_id == cid,
            InboundMessage.idpk.is_not(None),
        )).all()
        if keys:
            session.exec(delete(ProcessedIdpk).where(ProcessedIdpk.idpk.in_(keys)))
        session.exec(delete(WalletBalanceSnapshot).where(WalletBalanceSnapshot.cycle_id == cid))
        session.exec(delete(OutboundMessage).where(OutboundMessage.cycle_id == cid))
        session.exec(delete(InboundMessage).where(InboundMessage.cycle_id == cid))
        session.exec(delete(SwapRecord).where(SwapRecord.cycle_id == cid))
        session.exec(delete(OfferReservation).where(OfferReservation.offer_id == oid))
        session.exec(delete(MarketBid).where(MarketBid.cycle_id == cid))
        session.exec(delete(MarketOffer).where(MarketOffer.offer_id == oid))
        session.exec(delete(MarketCycle).where(MarketCycle.cycle_id == cid))
        session.exec(delete(Cycle).where(Cycle.cycle_id == cid))
        session.commit()


def payload(scenario, city, quantity=80, idpk=None):
    return {
        "idpk": str(idpk or uuid4()), "msgId": str(uuid4()),
        "type": "bid", "timestamp": datetime.now(timezone.utc).isoformat(),
        "cityId": city, "cycleId": scenario["cycle"],
        "data": {"offerId": scenario["offer"], "quantity": quantity},
    }


def _snapshot(s):
    with Session(engine) as session:
        reservations = session.exec(select(OfferReservation).where(
            OfferReservation.offer_id == s["offer"])).all()
        swaps = session.exec(select(SwapRecord).where(
            SwapRecord.cycle_id == s["cycle"],
            SwapRecord.role == "PROPOSER")).all()
        bids = session.exec(select(MarketBid).where(
            MarketBid.cycle_id == s["cycle"],
            MarketBid.direction == "INCOMING")).all()
        rejected = session.exec(select(OutboundMessage).where(
            OutboundMessage.cycle_id == s["cycle"],
            OutboundMessage.message_type == "bid-rejected")).all()
        return reservations, swaps, bids, rejected


def test_dos_bids_40ms_por_ingress_sin_sobreventa(scenario):
    barrier = Barrier(2)
    a = payload(scenario, "COR")
    b = payload(scenario, "HGW")

    def send(data, delay):
        barrier.wait(timeout=15)
        sleep(delay)
        with TestClient(app) as client:
            r = client.post("/internal/messages", json=data)
        assert r.status_code == 200, r.text
        return r.json()

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(send, a, 0)
        f2 = pool.submit(send, b, 0.040)
        assert f1.result(timeout=30)["status"] == "ok"
        assert f2.result(timeout=30)["status"] == "ok"

    reservations, swaps, bids, rejected = _snapshot(scenario)
    assert len(reservations) == 1
    assert reservations[0].status == "HOLD" and reservations[0].quantity == 80
    assert len(swaps) == 1
    assert swaps[0].give_asset == "KWH"
    assert swaps[0].give_amount == 80
    assert swaps[0].take_asset == "CR"
    assert swaps[0].take_amount == Decimal("340.00")
    assert swaps[0].idempotency_key == swaps[0].idpk == reservations[0].bid_idpk
    assert len([bid for bid in bids if bid.status == "RESERVED"]) == 1
    assert len([bid for bid in bids if bid.status == "REJECTED"]) == 1
    assert len(rejected) == 1
    assert rejected[0].payload["reason"] == "QUANTITY_UNAVAILABLE"
    assert rejected[0].payload["data"]["available"] == 20
    assert rejected[0].payload["data"]["requested"] == 80
    # El orden de aceptación entre hilos no está garantizado.
    # El rechazo debe dirigirse a la ciudad cuyo bid fue rechazado.
    bid_rechazado = next(
        bid for bid in bids
        if bid.status == "REJECTED"
    )

    assert rejected[0].routing_key == f"city.{bid_rechazado.counterpart_city_id}"
    assert rejected[0].payload["data"]["target"] == bid_rechazado.msg_id


def test_dos_sesiones_independientes_bloqueo_fila(scenario):
    # Sembrar bids válidos antes de reservar (camino de worker/reintentos).
    keys = [str(uuid4()), str(uuid4())]
    with Session(engine) as session:
        for key, city in zip(keys, ["COR", "HGW"]):
            session.add(MarketBid(
                idpk=key, msg_id=str(uuid4()), cycle_id=scenario["cycle"],
                direction="INCOMING", offer_id=scenario["offer"],
                counterpart_city_id=city, quantity=75, status="PENDING",
                created_at=scenario["now"],
            ))
        session.commit()

    barrier = Barrier(2)

    def accept(key, delay):
        barrier.wait(timeout=15)
        sleep(delay)
        with Session(engine) as session:
            result = reserve_incoming_bid(session, bid_idpk=key)
            session.commit()
        return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(accept, keys[0], 0)
        f2 = pool.submit(accept, keys[1], 0.040)
        results = [f1.result(timeout=30), f2.result(timeout=30)]

    assert sorted(r["status"] for r in results) == ["REJECTED", "RESERVED"]
    assert next(r for r in results if r["status"] == "REJECTED")["available"] == 25
    reservations, swaps, bids, rejected = _snapshot(scenario)
    assert len(reservations) == len(swaps) == len(rejected) == 1
    assert len(bids) == 2


def test_mismo_idpk_no_duplica_reserva_ni_swap(scenario):
    first = payload(scenario, "COR", 35)
    with TestClient(app) as client:
        response = client.post("/internal/messages", json=first)
        assert response.status_code == 200, response.text
    with Session(engine) as session:
        replay = reserve_incoming_bid(session, bid_idpk=first["idpk"])
        session.commit()
        assert replay["status"] == "RESERVED"
        assert replay["duplicate"] is True
    second = dict(first, msgId=str(uuid4()),
                  timestamp=datetime.now(timezone.utc).isoformat())
    with TestClient(app) as client:
        response = client.post("/internal/messages", json=second)
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "duplicate"
    reservations, swaps, _, rejected = _snapshot(scenario)
    assert len(reservations) == len(swaps) == 1
    assert len(rejected) == 0


def test_rollback_no_deja_reserva_huerfana(scenario):
    key = str(uuid4())
    with Session(engine) as session:
        session.add(MarketBid(
            idpk=key, msg_id=str(uuid4()), cycle_id=scenario["cycle"],
            direction="INCOMING", offer_id=scenario["offer"],
            counterpart_city_id="COR", quantity=60, status="PENDING",
            created_at=scenario["now"],
        ))
        session.commit()
        result = reserve_incoming_bid(session, bid_idpk=key)
        assert result["status"] == "RESERVED"
        session.rollback()
    reservations, swaps, bids, rejected = _snapshot(scenario)
    assert not reservations and not swaps and not rejected
    assert bids[0].status == "PENDING"


def test_wallet_rechazo_libera_y_permite_reservar_otro_bid(scenario):
    first = payload(scenario, "COR", 80)
    with TestClient(app) as client:
        assert client.post("/internal/messages", json=first).status_code == 200
    with Session(engine) as session:
        WalletService().process_swap_event(session, event_type="swap-rejected", data={
            "idpk": first["idpk"], "swapId": "sw-rejected-test", "reason": "INSUFFICIENT_ASSET",
        })
        session.commit()
    second = payload(scenario, "HGW", 80)
    with TestClient(app) as client:
        assert client.post("/internal/messages", json=second).status_code == 200
    reservations, swaps, bids, rejected = _snapshot(scenario)
    assert len(reservations) == 2
    assert sorted(res.status for res in reservations) == ["HOLD", "RELEASED"]
    assert len(swaps) == 2
    assert len([bid for bid in bids if bid.status == "RESERVED"]) == 2
    assert not rejected


def test_venta_no_descontada_antes_swap_settled(scenario):
    bid = payload(scenario, "COR", 40)
    with TestClient(app) as client:
        assert client.post("/internal/messages", json=bid).status_code == 200
    with Session(engine) as session:
        offer = session.exec(select(MarketOffer).where(
            MarketOffer.offer_id == scenario["offer"])).one()
        assert offer.remaining_quantity == 100  # HOLD no es settlement
        assert session.exec(select(OfferReservation).where(
            OfferReservation.offer_id == scenario["offer"])).one().status == "HOLD"
        assert session.exec(select(SwapRecord).where(
            SwapRecord.idpk == bid["idpk"])).one().status == "SUBMITTED"
