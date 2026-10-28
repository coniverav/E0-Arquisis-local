"""E2-039: respuesta al bid dirigida, target/cycle/city y duplicados."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.database import engine, run_migrations
from app.main import app
from app.models import Cycle, InboundMessage, MarketBid, OutboundMessage, ProcessedIdpk
from app.services.buy_bids import enqueue_buy_bid, renew_failed_bid_dispatches
from .test_buy_bids import seed


def setup_module():
    run_migrations()


def _bid():
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        cycle, offer = seed(session, now=now)
        result = enqueue_buy_bid(session, cycle_id=cycle, offer_id=offer,
                                 quantity=6, idpk=uuid4(), now=now)
    return cycle, offer, result


def _response(cycle, offer, target, *, message_type='bid-accepted', city='HGW',
              msg_id=None, idpk=None, quantity=6, reason='QUANTITY_UNAVAILABLE',
              amount=72, available=3):
    data = {'target': target, 'offerId': offer}
    message = {
        'idpk': str(idpk or uuid4()), 'msgId': str(msg_id or uuid4()),
        'type': message_type, 'timestamp': datetime.now(timezone.utc).isoformat(),
        'cityId': city, 'cycleId': cycle, 'data': data,
    }
    if message_type == 'bid-accepted':
        data.update({'quantity': quantity, 'pricePerEnergy': 12, 'amount': amount,
                     'swapId': 'sw-123', 'swapToken': 'opaque-test-token',
                     'expiresAt': (datetime.now(timezone.utc)+timedelta(minutes=2)).isoformat()})
    else:
        message['reason'] = reason
        message['code'] = {'QUANTITY_UNAVAILABLE': 409, 'OFFER_WITHDRAWN': 410,
                           'BID_TOO_SMALL': 422, 'MARKET_CLOSED': 410,
                           'SELF_TRADE': 422}[reason]
        data['requested'] = quantity
        if reason == 'QUANTITY_UNAVAILABLE':
            data['available'] = available
    return message


def _stored(key):
    with Session(engine) as session:
        return session.exec(select(MarketBid).where(MarketBid.idpk == key)).one()


def test_acceptance_correlates_without_wallet_ledger_side_effects():
    cycle, offer, bid = _bid()
    with Session(engine) as session:
        old_cycle = session.get(Cycle, cycle)
        budget, energy = old_cycle.budget_balance, old_cycle.energy_balance
    payload = _response(cycle, offer, bid['msgId'])
    response = TestClient(app).post('/internal/messages', json=payload)
    assert response.status_code == 200, response.text
    assert _stored(bid['idpk']).status == 'ACCEPTED'
    assert _stored(bid['idpk']).swap_id == 'sw-123'
    with Session(engine) as session:
        state = session.get(Cycle, cycle)
        assert state.budget_balance == budget and state.energy_balance == energy


def test_rejection_with_available_and_reason():
    cycle, offer, bid = _bid()
    response = TestClient(app).post('/internal/messages', json=_response(
        cycle, offer, bid['msgId'], message_type='bid-rejected', available=4))
    assert response.status_code == 200, response.text
    state = _stored(bid['idpk'])
    assert state.status == 'REJECTED'
    assert state.rejection_reason == 'QUANTITY_UNAVAILABLE'
    assert state.available_on_rejection == 4


def test_acceptance_amount_mismatch_rejected_conservatively():
    cycle, offer, bid = _bid()
    response = TestClient(app).post('/internal/messages', json=_response(
        cycle, offer, bid['msgId'], amount=70))
    assert response.status_code == 200, response.text
    assert response.json()['reason'] == 'ACCEPTANCE_AMOUNT_MISMATCH'
    assert _stored(bid['idpk']).status == 'PENDING'


def test_wrong_city_or_offer_does_not_resolve_bid():
    cycle, offer, bid = _bid()
    for payload in (
        _response(cycle, offer, bid['msgId'], city='COR'),
        _response(cycle, 'fake-offer', bid['msgId']),
        _response('other-cycle', offer, bid['msgId']),
    ):
        response = TestClient(app).post('/internal/messages', json=payload)
        assert response.status_code == 200, response.text
        assert response.json()['reason'] == 'BID_CORRELATION_MISMATCH'
    assert _stored(bid['idpk']).status == 'PENDING'


def test_unknown_bid_target_is_not_processed():
    cycle, offer, bid = _bid()
    response = TestClient(app).post('/internal/messages', json=_response(
        cycle, offer, str(uuid4())))
    assert response.status_code == 200, response.text
    assert response.json()['reason'] == 'UNKNOWN_BID_TARGET'
    assert _stored(bid['idpk']).status == 'PENDING'


def test_duplicate_idpk_with_new_msgid_is_business_idempotent():
    cycle, offer, bid = _bid()
    key = uuid4()

    first = _response(cycle, offer, bid['msgId'], idpk=key)
    assert TestClient(app).post(
        '/internal/messages', json=first
    ).status_code == 200

    # Reintento de la misma operación:
    # solo cambian msgId y timestamp, no los datos de negocio.
    second = dict(first)
    second['msgId'] = str(uuid4())
    second['timestamp'] = datetime.now(timezone.utc).isoformat()

    res = TestClient(app).post('/internal/messages', json=second)

    assert res.status_code == 200
    assert res.json()['status'] == 'duplicate'
    assert _stored(bid['idpk']).status == 'ACCEPTED'

    with Session(engine) as session:
        assert len(session.exec(select(ProcessedIdpk).where(
            ProcessedIdpk.idpk == str(key)
        )).all()) == 1


def test_late_conflicting_terminal_response_cannot_reverse_acceptance():
    cycle, offer, bid = _bid()
    assert TestClient(app).post('/internal/messages', json=_response(
        cycle, offer, bid['msgId'])).status_code == 200
    later = TestClient(app).post('/internal/messages', json=_response(
        cycle, offer, bid['msgId'], message_type='bid-rejected'))
    assert later.status_code == 200 and later.json()['reason'] == 'BID_ALREADY_RESOLVED'
    assert _stored(bid['idpk']).status == 'ACCEPTED'


def test_ack_is_not_a_commercial_acceptance():
    cycle, offer, bid = _bid()
    ack = {'idpk': str(uuid4()), 'msgId': str(uuid4()), 'type': 'ack',
           'timestamp': datetime.now(timezone.utc).isoformat(),
           'cityId': 'HGW', 'data': {'target': bid['msgId']}}
    result = TestClient(app).post('/internal/messages', json=ack)
    assert result.status_code == 200, result.text
    assert _stored(bid['idpk']).status == 'PENDING'


def test_reply_correlates_to_old_attempt_after_retry():
    cycle, offer, bid = _bid()
    with Session(engine) as session:
        previous = session.exec(select(OutboundMessage).where(
            OutboundMessage.msg_id == bid['msgId'])).one()
        previous.status = 'FAILED'
        session.commit()
        assert renew_failed_bid_dispatches(session, now=datetime.now(timezone.utc)) == 1
        session.commit()
    result = TestClient(app).post('/internal/messages', json=_response(
        cycle, offer, bid['msgId'], message_type='bid-rejected'))
    assert result.status_code == 200, result.text
    assert _stored(bid['idpk']).status == 'REJECTED'


def test_same_idpk_different_response_payload_is_not_a_valid_duplicate():
    cycle, offer, bid = _bid()
    key = uuid4()
    first = _response(cycle, offer, bid['msgId'], idpk=key)
    assert TestClient(app).post('/internal/messages', json=first).status_code == 200
    altered = _response(cycle, offer, bid['msgId'], idpk=key,
                        message_type='bid-rejected')
    response = TestClient(app).post('/internal/messages', json=altered)
    assert response.status_code == 200
    with Session(engine) as session:
        audit = session.exec(select(InboundMessage).where(
            InboundMessage.msg_id == altered['msgId'])).one()
        assert audit.status == 'NOT_PROCESSED'
        assert audit.reason == 'IDPK_PAYLOAD_CONFLICT'
    assert _stored(bid['idpk']).status == 'ACCEPTED'


def test_reply_cancels_pending_outbound_retries():
    cycle, offer, bid = _bid()
    response = TestClient(app).post('/internal/messages', json=_response(
        cycle, offer, bid['msgId'], message_type='bid-rejected'))
    assert response.status_code == 200
    with Session(engine) as session:
        attempts = session.exec(select(OutboundMessage).where(
            OutboundMessage.idpk == bid['idpk'],
            OutboundMessage.message_type == 'bid')).all()
        assert all(not attempt.dispatch_required for attempt in attempts)
