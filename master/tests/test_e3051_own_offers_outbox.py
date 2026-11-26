"""Ofertas propias AD8 con PostgreSQL efímero y sin red externa."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.database import engine
from app.main import app
from app.models import Cycle, MarketOffer, OfferReservation, OutboundMessage
from app.services.market_outbox import enqueue_market_envelope
from app.services.seller_offer_events import (
    change_local_offer_price, withdraw_local_offer, close_local_offers,
)
from app.services.wallet_service import WalletService
from tests.test_seller_offer_events import scenario, _settle


@pytest.fixture
def relay(monkeypatch):
    monkeypatch.setenv('E3_OWN_OFFERS_OUTBOX_ENABLED', 'true')
    return TestClient(app)


def messages(session):
    return session.exec(select(OutboundMessage).where(
        OutboundMessage.message_type.in_(('offer', 'offer-updated', 'offer-withdrawn'))
    ).order_by(OutboundMessage.id)).all()


def base_offer(scenario):
    now = scenario['now']
    with Session(engine) as session:
        row = enqueue_market_envelope(
            session, envelope={
                'msgId': str(uuid4()), 'idpk': str(uuid4()), 'type': 'offer',
                'cityId': 'KLD', 'cycleId': scenario['cycle_id'],
                'timestamp': now.isoformat(),
                'data': {'offerId': scenario['offer_id'], 'quantity': 1000,
                         'pricePerEnergy': 3.0},
            }, exchange_name='market', routing_key='market.offer.KLD',
            available_at=now, expires_at=now + timedelta(minutes=5), now=now,
        )
        session.commit()
        return row.id


def claim(client):
    response = client.post('/internal/outbox-relay/claims', json={
        'workerId': 'e3051-test', 'deliveryKind': 'BROKER', 'limit': 100,
    })
    assert response.status_code == 200
    return response.json()['items']


def confirm(client, item):
    return client.post(f"/internal/outbox-relay/{item['id']}/result", json={
        'claimToken': item['claimToken'], 'outcome': 'PUBLISHED',
    })


def test_crash_after_commit_lost_confirm_replays_before_update_and_withdraw(scenario, relay):
    base_offer(scenario)
    with Session(engine) as session:
        assert change_local_offer_price(session, offer_id=scenario['offer_id'], price=Decimal('4'))
        session.commit()  # caída antes de publicar
    with Session(engine) as session:
        assert not change_local_offer_price(session, offer_id=scenario['offer_id'], price=Decimal('4'))
        assert withdraw_local_offer(session, offer_id=scenario['offer_id'])
        session.commit()
    first = claim(relay)
    assert len(first) == 1 and first[0]['type'] == 'offer'
    assert claim(relay) == []  # las versiones posteriores esperan el confirm
    with Session(engine) as session:
        row = session.get(OutboundMessage, first[0]['id'])
        row.lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.add(row)
        session.commit()  # broker aceptó, confirmación local perdida
    replay = claim(relay)[0]
    assert replay['payload'] == first[0]['payload']
    assert (replay['idpk'], replay['msgId']) == (first[0]['idpk'], first[0]['msgId'])
    assert confirm(relay, first[0]).status_code == 409
    assert confirm(relay, replay).status_code == 200
    update = claim(relay)[0]
    assert update['type'] == 'offer-updated'
    assert update['payload']['data']['remaining'] == 1000
    assert confirm(relay, update).status_code == 200
    withdrawal = claim(relay)[0]
    assert withdrawal['type'] == 'offer-withdrawn'
    assert confirm(relay, withdrawal).status_code == 200
    assert claim(relay) == []
    with Session(engine) as session:
        offer = session.exec(select(MarketOffer)).one()
        assert offer.status == 'WITHDRAWN' and offer.remaining_quantity == 1000
        assert len(messages(session)) == 3


def test_rollback_of_price_and_withdraw_removes_both_intentions(scenario):
    with Session(engine) as session:
        change_local_offer_price(session, offer_id=scenario['offer_id'], price=Decimal('4'))
        withdraw_local_offer(session, offer_id=scenario['offer_id'])
        session.rollback()
    with Session(engine) as session:
        offer = session.exec(select(MarketOffer)).one()
        assert offer.status == 'ACTIVE' and offer.price_per_energy == Decimal('3')
        assert messages(session) == []


def test_relay_cannot_read_uncommitted_business_or_intent(scenario, relay):
    with Session(engine) as writer:
        change_local_offer_price(writer, offer_id=scenario['offer_id'], price=Decimal('4'))
        writer.flush()
        assert messages(writer)  # existe en la transacción del productor
        assert claim(relay) == []
        with Session(engine) as reader:
            assert reader.exec(select(MarketOffer)).one().price_per_energy == Decimal('3')
            assert messages(reader) == []
        writer.commit()
    item = claim(relay)[0]
    assert item['payload']['data']['pricePerEnergy'] == 4.0


@pytest.mark.parametrize('operation', ['price', 'withdraw'])
def test_no_changes_or_intentions_before_market_opens(scenario, operation):
    with Session(engine) as session:
        cycle = session.get(Cycle, scenario['cycle_id'])
        cycle.market_opens_at = datetime.now(timezone.utc) + timedelta(minutes=1)
        session.add(cycle)
        session.commit()
        with pytest.raises(ValueError):
            if operation == 'price':
                change_local_offer_price(session, offer_id=scenario['offer_id'], price=Decimal('4'))
            else:
                withdraw_local_offer(session, offer_id=scenario['offer_id'])
        session.commit()
        offer = session.exec(select(MarketOffer)).one()
        assert offer.status == 'ACTIVE' and offer.price_per_energy == Decimal('3')
        assert messages(session) == []


def test_offers_stay_with_legacy_dispatcher_by_default(scenario, relay, monkeypatch):
    monkeypatch.delenv('E3_OWN_OFFERS_OUTBOX_ENABLED')
    base_offer(scenario)
    assert claim(relay) == []
    legacy = relay.get('/internal/audit/outbound/dispatch')
    assert legacy.status_code == 200
    assert [item['type'] for item in legacy.json()['items']] == ['offer']


def test_stale_session_price_update_uses_remaining_after_settlement(scenario, relay):
    with Session(engine) as stale:
        cached = stale.exec(select(MarketOffer)).one()
        assert cached.remaining_quantity == 1000
        with Session(engine) as writer:
            event = _settle(writer, scenario)
            WalletService(client=object()).process_swap_event(writer, event_type='swap-settled', data=event)
            writer.commit()
        assert change_local_offer_price(stale, offer_id=scenario['offer_id'], price=Decimal('4'))
        stale.commit()
    with Session(engine) as session:
        updates = messages(session)
        assert [row.payload['data']['remaining'] for row in updates] == [800, 800]
        assert session.exec(select(MarketOffer)).one().remaining_quantity == 800
    first = claim(relay)[0]
    assert confirm(relay, first).status_code == 200
    second = claim(relay)[0]
    assert second['payload']['data']['remaining'] == 800


def test_withdraw_preserves_hold_and_settlement_after_withdraw(scenario):
    with Session(engine) as session:
        event = _settle(session, scenario)
        assert withdraw_local_offer(session, offer_id=scenario['offer_id'])
        session.commit()
        assert session.exec(select(OfferReservation)).one().status == 'HOLD'
    with Session(engine) as session:
        WalletService(client=object()).process_swap_event(session, event_type='swap-settled', data=event)
        session.commit()
        offer = session.exec(select(MarketOffer)).one()
        assert offer.status == 'WITHDRAWN' and offer.remaining_quantity == 800
        assert session.exec(select(OfferReservation)).one().status == 'COMMITTED'
        assert [row.message_type for row in messages(session)] == ['offer-withdrawn']


@pytest.mark.parametrize('deadline', ['lastOfferAt', 'market-closed'])
def test_cutoff_revokes_claim_and_pending_updates_without_scheduler(scenario, relay, deadline):
    base_offer(scenario)
    item = claim(relay)[0]
    with Session(engine) as session:
        change_local_offer_price(session, offer_id=scenario['offer_id'], price=Decimal('4'))
        if deadline == 'lastOfferAt':
            cycle = session.get(Cycle, scenario['cycle_id'])
            cycle.market_last_offer_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            session.add(cycle)
        else:
            close_local_offers(session, cycle_id=scenario['cycle_id'])
        session.commit()
    assert claim(relay) == []
    assert confirm(relay, item).status_code == 409
    with Session(engine) as session:
        assert all(not row.dispatch_required for row in messages(session))
        assert session.exec(select(MarketOffer)).one().remaining_quantity == 1000


def test_cutover_blocks_both_legacy_routes_and_rollback_keeps_identity(scenario, relay, monkeypatch):
    base_offer(scenario)
    with Session(engine) as session:
        row = messages(session)[0]
        msg_id, original = row.msg_id, row.payload
    assert relay.get('/internal/audit/outbound/dispatch').json()['items'] == []
    assert relay.post(f'/internal/audit/outbound/{msg_id}/reserve-quota').status_code == 409
    assert relay.post(f'/internal/audit/outbound/{msg_id}/result', json={'status': 'PUBLISHED'}).status_code == 409
    item = claim(relay)[0]
    monkeypatch.setenv('E3_OWN_OFFERS_OUTBOX_ENABLED', 'false')
    assert claim(relay) == []
    assert relay.get('/internal/audit/outbound/dispatch').json()['items'] == []
    with Session(engine) as session:
        row = session.get(OutboundMessage, item['id'])
        row.lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.add(row)
        session.commit()
    legacy = relay.get('/internal/audit/outbound/dispatch').json()['items']
    assert len(legacy) == 1 and legacy[0]['payload'] == original


def test_market_quota_defers_same_offer_intent(scenario, relay, monkeypatch):
    base_offer(scenario)
    monkeypatch.setattr('app.routers.outbox_relay.reserve_rate_limit',
                        lambda session, category, now: 7 if category == 'market:offer' else 0)
    assert claim(relay) == []
    with Session(engine) as session:
        row = messages(session)[0]
        assert row.dispatch_required and row.status == 'FAILED'
        assert row.last_error == 'OUTBOUND_RATE_LIMIT'
        assert row.available_at > datetime.now(timezone.utc)
        assert row.msg_id == row.payload['msgId'] and row.idpk == row.payload['idpk']
