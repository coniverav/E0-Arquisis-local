"""E2-029: settlement confirmado -> update, repricing, retiro, cierre y FIFO.

Ejecutar SOLO contra una base de pruebas (p.ej. energyshark_test_e2028).
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlmodel import Session, select

from app.config import CITY_ID
from app.database import engine, run_migrations
from app.main import app
from app.models import (
    Cycle, LedgerEntry, MarketOffer, OfferReservation, OutboundMessage,
    SwapRecord,
)
from app.services.seller_offer_events import (
    change_local_offer_price, withdraw_local_offer, close_local_offers,
)
from app.services.wallet_service import WalletService


@pytest.fixture(scope="module", autouse=True)
def migrated():
    run_migrations()


@pytest.fixture
def scenario():
    now = datetime.now(timezone.utc)
    cycle_id = f"e2029-{uuid4()}"
    offer_id = f"{CITY_ID}-{cycle_id}"
    with Session(engine) as session:
        session.add(Cycle(
            cycle_id=cycle_id, created_at=now,
            market_opens_at=now - timedelta(minutes=1),
            market_last_offer_at=now + timedelta(minutes=5),
            market_closes_at=now + timedelta(minutes=6),
            valid_until=now + timedelta(minutes=7),
        ))
        session.add(MarketOffer(
            offer_id=offer_id, cycle_id=cycle_id, city_id=CITY_ID,
            is_local=True, initial_quantity=1000, remaining_quantity=1000,
            price_per_energy=Decimal("3.00"), status="ACTIVE",
            published_at=now, last_updated_at=now,
        ))
        session.commit()
    yield {"cycle_id": cycle_id, "offer_id": offer_id, "now": now}
    with Session(engine) as session:
        session.exec(delete(OutboundMessage).where(
            OutboundMessage.cycle_id == cycle_id
        ))
        session.exec(delete(OfferReservation).where(
            OfferReservation.offer_id == offer_id
        ))
        session.exec(delete(SwapRecord).where(SwapRecord.cycle_id == cycle_id))
        session.exec(delete(LedgerEntry).where(LedgerEntry.cycle_id == cycle_id))
        session.exec(delete(MarketOffer).where(MarketOffer.offer_id == offer_id))
        session.exec(delete(Cycle).where(Cycle.cycle_id == cycle_id))
        session.commit()


def _events(session, cycle_id, message_type):
    return session.exec(select(OutboundMessage).where(
        OutboundMessage.cycle_id == cycle_id,
        OutboundMessage.message_type == message_type,
    ).order_by(OutboundMessage.id)).all()


def _settle(session, scenario):
    bid_idpk = str(uuid4())
    session.add(OfferReservation(
        offer_id=scenario["offer_id"], bid_idpk=bid_idpk,
        quantity=200, status="HOLD", created_at=scenario["now"],
    ))
    session.add(SwapRecord(
        idpk=bid_idpk, swap_id=f"sw-{uuid4()}",
        cycle_id=scenario["cycle_id"], role="PROPOSER",
        counterpart_city_id="COR", give_asset="KWH",
        give_amount=Decimal("200"), take_asset="CR",
        take_amount=Decimal("600"), memo=scenario["offer_id"],
        status="PROPOSED", idempotency_key=str(uuid4()),
        created_at=scenario["now"],
    ))
    session.flush()
    record = session.exec(select(SwapRecord).where(SwapRecord.idpk == bid_idpk)).one()
    event = {"idpk": bid_idpk, "swapId": record.swap_id}
    return event


def test_settlement_enqueues_update_only_once(scenario):
    with Session(engine) as session:
        event = _settle(session, scenario)
        service = WalletService()
        service.process_swap_event(session, event_type="swap-settled", data=event)
        service.process_swap_event(session, event_type="swap-settled", data=event)
        session.commit()

        offer = session.exec(select(MarketOffer).where(
            MarketOffer.offer_id == scenario["offer_id"]
        )).one()
        reservation = session.exec(select(OfferReservation).where(
            OfferReservation.bid_idpk == event["idpk"]
        )).one()
        updates = _events(session, scenario["cycle_id"], "offer-updated")
        assert offer.remaining_quantity == 800
        assert reservation.status == "COMMITTED"
        assert len(updates) == 1
        assert updates[0].payload["data"] == {
            "offerId": scenario["offer_id"], "remaining": 800,
            "pricePerEnergy": 3.0,
        }
        assert updates[0].exchange_name == "market"
        assert updates[0].routing_key == f"market.offer.{CITY_ID}"
        assert updates[0].status == "PENDING"


def test_reprice_creates_update_but_same_price_does_not(scenario):
    with Session(engine) as session:
        assert change_local_offer_price(session, offer_id=scenario["offer_id"], price=Decimal("4.50"))
        assert not change_local_offer_price(session, offer_id=scenario["offer_id"], price=Decimal("4.50"))
        session.commit()
        updates = _events(session, scenario["cycle_id"], "offer-updated")
        assert len(updates) == 1
        assert updates[0].payload["data"]["remaining"] == 1000
        assert updates[0].payload["data"]["pricePerEnergy"] == 4.5


def test_withdraw_is_terminal_and_idempotent(scenario):
    with Session(engine) as session:
        assert withdraw_local_offer(session, offer_id=scenario["offer_id"])
        assert not withdraw_local_offer(session, offer_id=scenario["offer_id"])
        with pytest.raises(ValueError):
            change_local_offer_price(session, offer_id=scenario["offer_id"], price=Decimal("5"))
        session.commit()
        offer = session.exec(select(MarketOffer).where(
            MarketOffer.offer_id == scenario["offer_id"]
        )).one()
        assert offer.status == "WITHDRAWN"
        withdrawn = _events(session, scenario["cycle_id"], "offer-withdrawn")
        assert len(withdrawn) == 1
        assert withdrawn[0].payload["data"] == {"offerId": scenario["offer_id"]}


def test_last_offer_cutoff_prevents_new_events(scenario):
    with Session(engine) as session:
        cycle = session.get(Cycle, scenario["cycle_id"])
        cycle.market_last_offer_at = scenario["now"] - timedelta(seconds=1)
        session.add(cycle)
        session.commit()
        with pytest.raises(ValueError, match="lastOfferAt"):
            withdraw_local_offer(session, offer_id=scenario["offer_id"])
        session.rollback()
        event = _settle(session, scenario)
        WalletService().process_swap_event(session, event_type="swap-settled", data=event)
        session.commit()
        offer = session.exec(select(MarketOffer).where(
            MarketOffer.offer_id == scenario["offer_id"]
        )).one()
        assert offer.remaining_quantity == 800  # confirma igualmente la venta
        assert not _events(session, scenario["cycle_id"], "offer-updated")


def test_central_close_closes_without_late_withdraw(scenario):
    with Session(engine) as session:
        assert close_local_offers(session, cycle_id=scenario["cycle_id"]) == 1
        assert close_local_offers(session, cycle_id=scenario["cycle_id"]) == 0
        session.commit()
        offer = session.exec(select(MarketOffer).where(
            MarketOffer.offer_id == scenario["offer_id"]
        )).one()
        assert offer.status == "CLOSED"
        assert not _events(session, scenario["cycle_id"], "offer-withdrawn")


def test_outbox_does_not_dispatch_update_before_base_offer(scenario):
    with Session(engine) as session:
        now = scenario["now"]
        base_msg = str(uuid4())
        base_idpk = str(uuid4())
        session.add(OutboundMessage(
            msg_id=base_msg, idpk=base_idpk, message_type="offer",
            cycle_id=scenario["cycle_id"], payload={
                "msgId": base_msg, "idpk": base_idpk, "type": "offer",
                "timestamp": now.isoformat().replace("+00:00", "Z"),
                "cityId": CITY_ID, "cycleId": scenario["cycle_id"],
                "data": {
                    "offerId": scenario["offer_id"],
                    "quantity": 1000, "pricePerEnergy": 3.0,
                },
            },
            exchange_name="market", routing_key=f"market.offer.{CITY_ID}",
            status="PENDING", attempt_count=0, dispatch_required=True,
            created_at=now - timedelta(seconds=1),
            expires_at=now + timedelta(minutes=5),
        ))
        session.flush()
        change_local_offer_price(session, offer_id=scenario["offer_id"], price=Decimal("4"))
        session.commit()

    client = TestClient(app)
    response = client.get("/internal/audit/outbound/dispatch", params={"limit": 100})
    assert response.status_code == 200, response.text
    mine = [i for i in response.json()["items"] if i["payload"].get("cycleId") == scenario["cycle_id"]]
    assert [i["type"] for i in mine] == ["offer"]

    with Session(engine) as session:
        base = session.exec(select(OutboundMessage).where(OutboundMessage.msg_id == base_msg)).one()
        base.status = "PUBLISHED"
        base.dispatch_required = False
        session.add(base)
        session.commit()

    response = client.get("/internal/audit/outbound/dispatch", params={"limit": 100})
    assert response.status_code == 200, response.text
    mine = [i for i in response.json()["items"] if i["payload"].get("cycleId") == scenario["cycle_id"]]
    assert [i["type"] for i in mine] == ["offer-updated"]


def test_authenticated_routes_reprice_then_withdraw(scenario):
    """La API funciona con una identidad verificada simulada (sin JWKS externo)."""
    from app.auth import verify_jwt
    app.dependency_overrides[verify_jwt] = lambda: {"sub": "pytest-operator"}
    try:
        client = TestClient(app)
        price_url = f'/market/offers/{scenario["offer_id"]}/price'
        retire_url = f'/market/offers/{scenario["offer_id"]}/withdraw'
        response = client.patch(price_url, json={"pricePerEnergy": "4.75"})
        assert response.status_code == 200, response.text
        assert response.json()["changed"] is True
        response = client.post(retire_url)
        assert response.status_code == 200, response.text
        assert response.json()["changed"] is True
        with Session(engine) as session:
            assert len(_events(session, scenario["cycle_id"], "offer-updated")) == 1
            assert len(_events(session, scenario["cycle_id"], "offer-withdrawn")) == 1
    finally:
        app.dependency_overrides.pop(verify_jwt, None)
