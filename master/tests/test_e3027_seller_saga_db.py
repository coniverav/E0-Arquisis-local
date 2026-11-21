"""E3-027: pruebas con PostgreSQL real + código productivo AD4/AD8.

Nunca invoca AWS/Wallet/RabbitMQ; StartExecution es una intención persistente.
El runner apunta exclusivamente a una base energyshark_test_*.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from threading import Barrier
from time import sleep
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.config import CITY_ID
from app.database import engine
from app.main import app
from app.models import (
    Cycle, MarketBid, MarketOffer, OfferReservation, OutboundMessage, SwapRecord,
)
from app.services.outbox_claims import claim_batch, finish_claim
from app.services.seller_bid_acceptance import reserve_incoming_bid
from app.services.seller_bid_policy import process_due_seller_bids
from app.services.seller_swap_dispatch import _claim_one

ARN = 'arn:aws:states:us-east-1:123456789012:stateMachine:energyshark-seller-e3'


@pytest.fixture
def seller_config(monkeypatch):
    monkeypatch.setenv('E3_SELLER_SAGA_ENABLED', 'true')
    monkeypatch.setenv('E3_SELLER_STATE_MACHINE_ARN', ARN)


@pytest.fixture
def scenario():
    moment = datetime.now(timezone.utc)
    cycle_id = 'e3027-' + str(uuid4())
    offer_id = f'{CITY_ID}-{cycle_id}'
    with Session(engine) as session:
        session.add(Cycle(
            cycle_id=cycle_id, created_at=moment, status_idpk=str(uuid4()),
            market_opens_at=moment - timedelta(minutes=1),
            market_last_offer_at=moment + timedelta(minutes=4),
            market_last_bid_at=moment + timedelta(minutes=3),
            market_closes_at=moment + timedelta(minutes=5),
            valid_until=moment + timedelta(minutes=6),
            status_payload={'market': {'minBid': 5}},
        ))
        session.add(MarketOffer(
            offer_id=offer_id, cycle_id=cycle_id, city_id=CITY_ID,
            is_local=True, initial_quantity=100, remaining_quantity=100,
            price_per_energy=Decimal('4.25'), status='ACTIVE',
            published_at=moment, last_updated_at=moment,
        ))
        session.commit()
    return {'now': moment, 'cycle': cycle_id, 'offer': offer_id}


def _seed_bid(ctx, *, quantity=40, city='COR', idpk=None):
    key = idpk or str(uuid4())
    with Session(engine) as session:
        session.add(MarketBid(
            idpk=key, msg_id=str(uuid4()), cycle_id=ctx['cycle'],
            direction='INCOMING', offer_id=ctx['offer'],
            counterpart_city_id=city, quantity=quantity,
            status='PENDING', created_at=ctx['now'],
        ))
        session.commit()
    return key


def _post_bid(ctx, *, quantity=40, city='COR', idpk=None, msg_id=None):
    payload = {
        'idpk': idpk or str(uuid4()), 'msgId': msg_id or str(uuid4()),
        'type': 'bid', 'timestamp': datetime.now(timezone.utc).isoformat(),
        'cycleId': ctx['cycle'], 'cityId': city,
        'data': {'offerId': ctx['offer'], 'quantity': quantity},
    }
    with TestClient(app) as client:
        response = client.post('/internal/messages', json=payload)
    assert response.status_code == 200, response.text
    return payload, response.json()


def _snapshot(ctx):
    with Session(engine) as session:
        return {
            'bids': session.exec(select(MarketBid).where(MarketBid.cycle_id == ctx['cycle'])).all(),
            'holds': session.exec(select(OfferReservation).where(OfferReservation.offer_id == ctx['offer'])).all(),
            'swaps': session.exec(select(SwapRecord).where(SwapRecord.cycle_id == ctx['cycle'])).all(),
            'outbox': session.exec(select(OutboundMessage).where(
                OutboundMessage.cycle_id == ctx['cycle'],
                OutboundMessage.delivery_kind == 'STEP_FUNCTIONS',
            )).all(),
        }


def test_fifo_ingress_writes_hold_swap_bid_and_start_atomically(seller_config, scenario):
    payload, response = _post_bid(scenario, quantity=40)
    assert response['status'] == 'ok'
    state = _snapshot(scenario)
    assert len(state['holds']) == len(state['swaps']) == len(state['outbox']) == 1
    assert len(state['bids']) == 1
    assert state['bids'][0].status == 'RESERVED'
    assert state['holds'][0].status == 'HOLD' and state['holds'][0].quantity == 40
    assert state['swaps'][0].status == 'SUBMITTED'
    row = state['outbox'][0]
    assert row.status == 'PENDING' and row.dispatch_required
    assert row.message_type == 'SAGA_START' and row.delivery_kind == 'STEP_FUNCTIONS'
    assert row.state_machine_arn == ARN
    assert row.execution_name == row.idpk == payload['idpk']
    assert row.expires_at is None  # debe poder compensar tras closesAt
    data = json.loads(row.execution_input)
    assert data['idpk'] == payload['idpk']
    assert data['bid']['msgId'] == payload['msgId']
    assert data['swap']['take']['amount'] == '170.00'
    assert data['marketClosesAt'] == (
        scenario['now'] + timedelta(minutes=5)
    ).isoformat().replace('+00:00', 'Z')
    # Un evento Wallet aún no llegó: nunca generar bid-accepted por la reserva.
    assert state['bids'][0].status != 'ACCEPTED'


def test_crash_after_commit_new_process_can_claim_original_intent(seller_config, scenario):
    key = _seed_bid(scenario, quantity=60)
    with Session(engine) as session:
        result = reserve_incoming_bid(session, bid_idpk=key)
        assert result['status'] == 'RESERVED'
        session.commit()
    # Simula caída del proceso productor: se cierran todas las sesiones.
    with Session(engine) as session:
        row = session.exec(select(OutboundMessage).where(
            OutboundMessage.idpk == key,
            OutboundMessage.message_type == 'SAGA_START',
        )).one()
        stored = (row.id, row.msg_id, row.idpk, row.execution_input)
    moment = datetime.now(timezone.utc)
    with Session(engine) as relay_session:
        claimed = claim_batch(relay_session, worker_id='relay-after-crash',
                              kinds=('STEP_FUNCTIONS',), now=moment)
        assert len(claimed) == 1
        token = claimed[0].claim_token
        assert (claimed[0].id, claimed[0].msg_id, claimed[0].idpk,
                claimed[0].execution_input) == stored
        relay_session.commit()
    # Simular nuevo crash en medio del envio externo y recuperacion por lease.
    with Session(engine) as relay_session:
        recovered = claim_batch(relay_session, worker_id='relay-recovered',
                                kinds=('STEP_FUNCTIONS',), now=moment + timedelta(seconds=31))
        assert len(recovered) == 1
        assert recovered[0].claim_token != token
        assert recovered[0].execution_input == stored[3]
        next_token = recovered[0].claim_token
        relay_session.commit()
    with Session(engine) as session:
        assert not finish_claim(session, row_id=stored[0], claim_token=token,
                                outcome='STARTED', now=moment + timedelta(seconds=32))
        assert finish_claim(session, row_id=stored[0], claim_token=next_token,
                            outcome='STARTED', now=moment + timedelta(seconds=32))
        session.commit()
    with Session(engine) as session:
        row = session.get(OutboundMessage, stored[0])
        assert row.status == 'STARTED' and not row.dispatch_required
        assert row.execution_input == stored[3] and row.msg_id == stored[1]


def test_duplicate_bid_does_not_enqueue_duplicate_execution(seller_config, scenario):
    payload, answer = _post_bid(scenario, quantity=40)
    assert answer['status'] == 'ok'
    repeated = dict(payload, msgId=str(uuid4()),
                    timestamp=datetime.now(timezone.utc).isoformat())
    with TestClient(app) as client:
        response = client.post('/internal/messages', json=repeated)
    assert response.status_code == 200
    with Session(engine) as session:
        again = reserve_incoming_bid(session, bid_idpk=payload['idpk'])
        assert again['duplicate'] is True
        session.commit()
    state = _snapshot(scenario)
    assert len(state['holds']) == len(state['swaps']) == len(state['outbox']) == 1
    assert state['outbox'][0].idpk == payload['idpk']


def test_rollback_erases_hold_swap_and_execution_intent(seller_config, scenario):
    key = _seed_bid(scenario, quantity=40)
    with Session(engine) as session:
        result = reserve_incoming_bid(session, bid_idpk=key)
        assert result['status'] == 'RESERVED'
        session.rollback()
    state = _snapshot(scenario)
    assert not state['holds'] and not state['swaps'] and not state['outbox']
    assert state['bids'][0].status == 'PENDING'


def test_outbox_failure_aborts_entire_local_acceptance(monkeypatch, seller_config, scenario):
    key = _seed_bid(scenario, quantity=40)
    def outage(*args, **kwargs):
        raise RuntimeError('simulated PostgreSQL/outbox failure')
    monkeypatch.setattr('app.services.seller_saga_start.enqueue_saga_start', outage)
    with Session(engine) as session:
        with pytest.raises(RuntimeError, match='simulated'):
            reserve_incoming_bid(session, bid_idpk=key)
        session.rollback()
    state = _snapshot(scenario)
    assert not state['holds'] and not state['swaps'] and not state['outbox']
    assert state['bids'][0].status == 'PENDING'


def test_missing_arn_fails_closed_and_rolls_back(monkeypatch, seller_config, scenario):
    key = _seed_bid(scenario, quantity=40)
    monkeypatch.delenv('E3_SELLER_STATE_MACHINE_ARN')
    with Session(engine) as session:
        with pytest.raises(ValueError, match='E3_SELLER_STATE_MACHINE_ARN_REQUIRED'):
            reserve_incoming_bid(session, bid_idpk=key)
        session.rollback()
    state = _snapshot(scenario)
    assert not state['holds'] and not state['swaps'] and not state['outbox']
    assert state['bids'][0].status == 'PENDING'


def test_old_e2_mode_unchanged_with_flag_off(monkeypatch, scenario):
    monkeypatch.setenv('E3_SELLER_SAGA_ENABLED', 'false')
    monkeypatch.delenv('E3_SELLER_STATE_MACHINE_ARN', raising=False)
    key = _seed_bid(scenario, quantity=40)
    with Session(engine) as session:
        result = reserve_incoming_bid(session, bid_idpk=key)
        assert result['status'] == 'RESERVED'
        session.commit()
    state = _snapshot(scenario)
    assert len(state['holds']) == len(state['swaps']) == 1
    assert not state['outbox']
    with Session(engine) as session:
        old_worker_claim = _claim_one(session, now=datetime.now(timezone.utc))
        assert old_worker_claim is not None and old_worker_claim.idpk == key
        session.rollback()


def test_wallet_e2_worker_excludes_saga_owned_swap_even_after_failed_outbox(seller_config, scenario):
    key = _seed_bid(scenario, quantity=40)
    with Session(engine) as session:
        reserve_incoming_bid(session, bid_idpk=key)
        session.commit()
    with Session(engine) as session:
        assert _claim_one(session, now=datetime.now(timezone.utc)) is None
        row = session.exec(select(OutboundMessage).where(
            OutboundMessage.idpk == key,
            OutboundMessage.message_type == 'SAGA_START',
        )).one()
        row.status = 'FAILED'
        session.add(row)
        session.commit()
    with Session(engine) as session:
        assert _claim_one(session, now=datetime.now(timezone.utc)) is None


def test_concurrent_bids_only_one_saga_and_no_overbooking(seller_config, scenario):
    barrier = Barrier(2)
    def buyer(city, gap):
        barrier.wait(timeout=15)
        sleep(gap)
        return _post_bid(scenario, city=city, quantity=80)
    with ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(buyer, 'COR', 0)
        f2 = executor.submit(buyer, 'HGW', .040)
        answers = [f1.result(timeout=45)[1], f2.result(timeout=45)[1]]
    assert all(answer['status'] == 'ok' for answer in answers)
    state = _snapshot(scenario)
    assert len(state['holds']) == len(state['swaps']) == len(state['outbox']) == 1
    assert sum(h.quantity for h in state['holds'] if h.status == 'HOLD') == 80
    assert len([b for b in state['bids'] if b.status == 'REJECTED']) == 1
    assert len([b for b in state['bids'] if b.status == 'RESERVED']) == 1
    assert state['outbox'][0].idpk == state['swaps'][0].idpk


def test_due_policy_uses_same_reserve_hook(seller_config, scenario):
    key = _seed_bid(scenario, quantity=40)
    with Session(engine) as session:
        result = process_due_seller_bids(session, now=datetime.now(timezone.utc))
        assert result['reserved'] == 1
        session.commit()
    state = _snapshot(scenario)
    assert len(state['outbox']) == 1
    assert state['outbox'][0].idpk == key


def test_two_independent_reservations_create_two_distinct_sagas(seller_config, scenario):
    keys = [_seed_bid(scenario, quantity=40, city='COR'),
            _seed_bid(scenario, quantity=50, city='HGW')]
    for key in keys:
        with Session(engine) as session:
            assert reserve_incoming_bid(session, bid_idpk=key)['status'] == 'RESERVED'
            session.commit()
    state = _snapshot(scenario)
    assert len(state['outbox']) == len(state['holds']) == 2
    assert {row.execution_name for row in state['outbox']} == set(keys)
    assert all(row.state_machine_arn == ARN for row in state['outbox'])


def test_rejected_bid_does_not_create_saga(seller_config, scenario):
    key = _seed_bid(scenario, quantity=101)
    with Session(engine) as session:
        result = reserve_incoming_bid(session, bid_idpk=key)
        assert result['status'] == 'REJECTED' and result['reason'] == 'QUANTITY_UNAVAILABLE'
        session.commit()
    state = _snapshot(scenario)
    assert not state['holds'] and not state['swaps'] and not state['outbox']


def test_awaiting_closes_at_does_not_expire_start_intent(seller_config, scenario):
    key = _seed_bid(scenario, quantity=25)
    with Session(engine) as session:
        reserve_incoming_bid(session, bid_idpk=key)
        session.commit()
    cycle_close = scenario['now'] + timedelta(minutes=5)
    with Session(engine) as session:
        claimed = claim_batch(session, worker_id='late-recovery', kinds=('STEP_FUNCTIONS',),
                              now=cycle_close + timedelta(minutes=1))
        assert len(claimed) == 1
        assert claimed[0].idpk == key
        session.rollback()
