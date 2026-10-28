"""E2-038: outbox dirigida, reintento AD3 y validación antes del envío."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.database import engine, run_migrations
from app.main import app
from app.models import (
    Cycle, DistanceTable, MarketBid, MarketCycle, MarketFeedOffer, MarketOffer,
    OutboundMessage, StrategyConfig,
)
from app.services.buy_bids import enqueue_buy_bid, renew_failed_bid_dispatches

NOW = datetime(2026, 10, 27, 15, 0, tzinfo=timezone.utc)


def setup_module():
    run_migrations()


def seed(session, *, now=NOW, remaining=20, min_bid=3, last_bid=None,
         span=0, enabled=True):
    cycle = f'e2038-{uuid4()}'
    offer_id = f'HGW-{uuid4()}'
    version = uuid4().int % 1000000000 + 1000000000
    last_bid = last_bid if last_bid is not None else now + timedelta(minutes=3)
    session.add(StrategyConfig(
        version=version, status='ACTIVE', offer_selection_policy='CHEAPEST_EFFECTIVE_PRICE',
        span_seconds=span, offer_price_markup=Decimal('1.2'),
        offer_quantity_fraction=Decimal('1'), bid_acceptance_policy='FIFO',
        created_at=now,
    ))
    session.flush()
    session.add(Cycle(
        cycle_id=cycle, status_idpk=str(uuid4()),
        status_payload={'market': {'minBid': min_bid}},
        market_opens_at=now - timedelta(minutes=2),
        market_last_bid_at=last_bid, market_closes_at=now + timedelta(minutes=5),
        strategy_version=version, created_at=now,
    ))
    session.add(MarketCycle(cycle_id=cycle))
    session.flush()
    session.add(MarketFeedOffer(
        cycle_id=cycle, city_id='HGW', offer_id=offer_id,
        initial_quantity=25, remaining=remaining, price_per_energy=Decimal('12'),
        state='ACTIVE', uncertain=False, last_event_at=now - timedelta(seconds=10),
        last_event_msg_id=str(uuid4()),
    ))
    session.add(DistanceTable(
        msg_id=str(uuid4()), idpk=str(uuid4()), cycle_id=cycle,
        source_timestamp=now-timedelta(seconds=4), received_at=now,
        disabled_route_transport_cost=Decimal('10'),
        distances={'HGW': {'distance': 1000, 'transportCost': 1, 'enabled': enabled}},
    ))
    session.flush()
    return cycle, offer_id


def test_persists_bid_and_outbox_atomically_without_price():
    with Session(engine) as session:
        cycle, offer = seed(session)
        key = uuid4()
        result = enqueue_buy_bid(session, cycle_id=cycle, offer_id=offer,
                                 quantity=8, idpk=key, now=NOW)
        outbound = session.exec(select(OutboundMessage).where(
            OutboundMessage.msg_id == result['msgId'])).one()
        bid = session.exec(select(MarketBid).where(MarketBid.idpk == str(key))).one()
        reference = session.exec(select(MarketOffer).where(MarketOffer.offer_id == offer)).one()
        assert bid.idpk == outbound.idpk == str(key)
        assert reference.city_id == 'HGW' and reference.is_local is False
        assert outbound.payload['data'] == {'offerId': offer, 'quantity': 8}
        assert 'price' not in str(outbound.payload).lower()
        assert outbound.routing_key == 'city.HGW'
        assert outbound.exchange_name == 'energy.x'
        assert outbound.dispatch_required is True
        assert outbound.expires_at == NOW + timedelta(minutes=3)
        assert bid.status == 'PENDING'


def test_same_http_idpk_returns_existing_bid_without_new_outbox():
    with Session(engine) as session:
        cycle, offer = seed(session)
        key = uuid4()
        first = enqueue_buy_bid(session, cycle_id=cycle, offer_id=offer,
                                quantity=8, idpk=key, now=NOW)
        repeat = enqueue_buy_bid(session, cycle_id=cycle, offer_id=offer,
                                 quantity=8, idpk=key, now=NOW + timedelta(minutes=10))
        assert repeat['duplicate'] is True
        assert repeat['msgId'] == first['msgId']
        assert len(session.exec(select(OutboundMessage).where(
            OutboundMessage.idpk == str(key))).all()) == 1


def test_same_idpk_cannot_change_quantity():
    with Session(engine) as session:
        cycle, offer = seed(session)
        key = uuid4()
        enqueue_buy_bid(session, cycle_id=cycle, offer_id=offer,
                        quantity=5, idpk=key, now=NOW)
        with pytest.raises(HTTPException) as err:
            enqueue_buy_bid(session, cycle_id=cycle, offer_id=offer,
                            quantity=6, idpk=key, now=NOW)
        assert err.value.status_code == 409


def test_block_outside_window_and_quantity_constraints():
    with Session(engine) as session:
        cycle, offer = seed(session, min_bid=4)
        for qty, time in [(3, NOW), (100, NOW), (8, NOW+timedelta(minutes=4))]:
            with pytest.raises(HTTPException) as err:
                enqueue_buy_bid(session, cycle_id=cycle, offer_id=offer,
                                quantity=qty, idpk=uuid4(), now=time)
            assert err.value.status_code == 409
        session.rollback()


def test_pending_local_bids_cannot_overcommit_snapshot():
    with Session(engine) as session:
        cycle, offer = seed(session, remaining=10)
        enqueue_buy_bid(session, cycle_id=cycle, offer_id=offer,
                        quantity=7, idpk=uuid4(), now=NOW)
        with pytest.raises(HTTPException) as err:
            enqueue_buy_bid(session, cycle_id=cycle, offer_id=offer,
                            quantity=4, idpk=uuid4(), now=NOW)
        assert err.value.status_code == 409


def test_disabled_route_can_buy_with_penalty():
    with Session(engine) as session:
        cycle, offer = seed(session, enabled=False)
        result = enqueue_buy_bid(session, cycle_id=cycle, offer_id=offer,
                                 quantity=5, idpk=uuid4(), now=NOW)
        assert result['status'] == 'PENDING'


def test_failed_publish_generates_new_msgid_same_idpk():
    with Session(engine) as session:
        cycle, offer = seed(session)
        result = enqueue_buy_bid(session, cycle_id=cycle, offer_id=offer,
                                 quantity=6, idpk=uuid4(), now=NOW)
        original = session.exec(select(OutboundMessage).where(
            OutboundMessage.msg_id == result['msgId'])).one()
        original.status = 'FAILED'
        session.commit()
        assert renew_failed_bid_dispatches(session, now=NOW+timedelta(seconds=5)) == 1
        session.commit()
        new = session.exec(select(OutboundMessage).where(
            OutboundMessage.idpk == result['idpk'],
            OutboundMessage.dispatch_required.is_(True))).one()
        bid = session.exec(select(MarketBid).where(MarketBid.idpk == result['idpk'])).one()
        assert new.msg_id != original.msg_id and bid.msg_id == new.msg_id
        assert new.idpk == original.idpk and new.payload['idpk'] == original.idpk
        assert original.dispatch_required is False
        assert renew_failed_bid_dispatches(session, now=NOW+timedelta(seconds=6)) == 0


def test_expired_failed_bid_not_republished():
    with Session(engine) as session:
        cycle, offer = seed(session)
        result = enqueue_buy_bid(session, cycle_id=cycle, offer_id=offer,
                                 quantity=5, idpk=uuid4(), now=NOW)
        outbound = session.exec(select(OutboundMessage).where(
            OutboundMessage.msg_id == result['msgId'])).one()
        outbound.status = 'FAILED'
        session.commit()
        assert renew_failed_bid_dispatches(session, now=NOW+timedelta(minutes=4)) == 0
        assert outbound.dispatch_required is False


def test_post_internal_endpoint_creates_bid():
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        cycle, offer = seed(session, now=now)
        session.commit()
    response = TestClient(app).post('/internal/market/bids', json={
        'cycleId': cycle, 'offerId': offer, 'quantity': 5, 'idpk': str(uuid4()),
    })
    assert response.status_code == 201, response.text
    assert response.json()['routingKey'] == 'city.HGW'
    assert response.json()['status'] == 'PENDING'
