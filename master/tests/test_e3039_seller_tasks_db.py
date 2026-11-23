"""E3-039: Lambda seller lee la reserva/rechazo AD4 de PostgreSQL real.

Requiere DATABASE_URL apuntando a energyshark_test_*; sin AWS/RabbitMQ.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import sys
from uuid import uuid4

import pytest
from sqlmodel import Session, select

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "infra" / "cdk" / "lambda_src" / "saga_task"))

from postgres_store import PostgresStore
from seller_reservation import (
    SellerReservationInconsistent, verify_seller_reservation,
    verify_seller_rejection,
)
from app.config import CITY_ID
from app.database import engine
from app.models import (
    Cycle, MarketBid, MarketOffer, OfferReservation, OutboundMessage,
    SagaExecution, SwapRecord,
)
from app.services.seller_bid_acceptance import reserve_incoming_bid

ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:energyshark-seller-e3"


@pytest.fixture
def ctx(monkeypatch):
    monkeypatch.setenv("E3_SELLER_SAGA_ENABLED", "true")
    monkeypatch.setenv("E3_SELLER_STATE_MACHINE_ARN", ARN)
    moment = datetime.now(timezone.utc)
    cycle = "e3039-" + str(uuid4())
    offer = f"{CITY_ID}-{cycle}"
    bid = str(uuid4())
    with Session(engine) as session:
        session.add(Cycle(
            cycle_id=cycle, created_at=moment, status_idpk=str(uuid4()),
            market_opens_at=moment - timedelta(minutes=1),
            market_last_offer_at=moment + timedelta(minutes=4),
            market_last_bid_at=moment + timedelta(minutes=3),
            market_closes_at=moment + timedelta(minutes=5),
            valid_until=moment + timedelta(minutes=6),
            status_payload={"market": {"minBid": 5}},
        ))
        session.add(MarketOffer(
            offer_id=offer, cycle_id=cycle, city_id=CITY_ID,
            is_local=True, initial_quantity=100, remaining_quantity=100,
            price_per_energy=Decimal("4.25"), status="ACTIVE",
            published_at=moment, last_updated_at=moment,
        ))
        session.add(MarketBid(
            idpk=bid, msg_id=str(uuid4()), cycle_id=cycle,
            direction="INCOMING", offer_id=offer, counterpart_city_id="COR",
            quantity=40, status="PENDING", created_at=moment,
        ))
        session.commit()
    # El adaptador psycopg usa la misma BD test que SQLModel.
    dsn = engine.url.render_as_string(hide_password=False)
    store = PostgresStore(dsn.replace("postgresql+psycopg://", "postgresql://", 1),
                          city_id=CITY_ID)
    return {"now": moment, "cycle": cycle, "offer": offer, "bid": bid,
            "store": store}


def _decide(ctx, quantity):
    with Session(engine) as session:
        bid = session.exec(select(MarketBid).where(MarketBid.idpk == ctx["bid"])).one()
        bid.quantity = quantity
        session.add(bid)
        session.commit()
    with Session(engine) as session:
        result = reserve_incoming_bid(session, bid_idpk=ctx["bid"], now=ctx["now"])
        session.commit()
    return result


def _inventory(ctx):
    with Session(engine) as session:
        return {
            "holds": len(session.exec(select(OfferReservation)).all()),
            "swaps": len(session.exec(select(SwapRecord)).all()),
            "sagas": len(session.exec(select(SagaExecution)).all()),
            "starts": len(session.exec(select(OutboundMessage).where(
                OutboundMessage.message_type == "SAGA_START"
            )).all()),
            "rejections": len(session.exec(select(OutboundMessage).where(
                OutboundMessage.message_type == "bid-rejected"
            )).all()),
        }


def test_reserve_lambda_reuses_original_hold_and_saga(ctx):
    assert _decide(ctx, 40)["status"] == "RESERVED"
    before = _inventory(ctx)
    params = dict(
        bid_idpk=ctx["bid"], cycle_id=ctx["cycle"], offer_id=ctx["offer"],
        quantity=40, saga_execution_id=ctx["bid"],
    )
    first = verify_seller_reservation(ctx["store"], **params)
    second = verify_seller_reservation(ctx["store"], **params)
    assert first == second
    assert first == {"status": "RESERVED", "bidIdpk": ctx["bid"],
                     "quantity": 40, "alreadyReserved": True, "availableAfter": 60}
    assert before == _inventory(ctx)
    assert before == {"holds": 1, "swaps": 1, "sagas": 1,
                      "starts": 1, "rejections": 0}


@pytest.mark.parametrize("quantity, reason", [(101, "QUANTITY_UNAVAILABLE"),
                                            (4, "BID_TOO_SMALL")])
def test_rejected_bid_has_single_traced_response_and_no_saga(ctx, quantity, reason):
    decision = _decide(ctx, quantity)
    assert decision["status"] == "REJECTED" and decision["reason"] == reason
    first = verify_seller_rejection(ctx["store"], bid_idpk=ctx["bid"], reason=reason)
    second = verify_seller_rejection(ctx["store"], bid_idpk=ctx["bid"], reason=reason)
    assert first == second and first["outboxVerified"]
    assert _inventory(ctx) == {"holds": 0, "swaps": 0, "sagas": 0,
                               "starts": 0, "rejections": 1}
    with Session(engine) as session:
        duplicate = reserve_incoming_bid(session, bid_idpk=ctx["bid"], now=ctx["now"])
        session.commit()
    assert duplicate["duplicate"]
    assert _inventory(ctx)["rejections"] == 1


def test_lambda_fails_closed_on_inconsistent_hold(ctx):
    _decide(ctx, 40)
    with Session(engine) as session:
        reservation = session.exec(select(OfferReservation).where(
            OfferReservation.bid_idpk == ctx["bid"]
        )).one()
        reservation.status = "RELEASED"  # inconsistencia simulada, sin evidencia Wallet
        session.add(reservation)
        session.commit()
    with pytest.raises(SellerReservationInconsistent, match="SELLER_HOLD_MISSING_OR_INCONSISTENT"):
        verify_seller_reservation(
            ctx["store"], bid_idpk=ctx["bid"], cycle_id=ctx["cycle"],
            offer_id=ctx["offer"], quantity=40, saga_execution_id=ctx["bid"],
        )
    assert _inventory(ctx)["starts"] == 1  # no se inventa otro inicio
