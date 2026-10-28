"""E2-033: propuesta Wallet confirmada -> respuesta bid-accepted, nunca HTTP 202.

Estas pruebas usan PostgreSQL de tests y el ingress real para los eventos.
El JWT de pruebas va firmado ES256, aunque su firma no se verifica aquí:
según el contrato la verificación de firma contra JWKS es del comprador.
"""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.config import CITY_ID
from app.database import engine, run_migrations
from app.main import app
from app.models import MarketBid, MarketOffer, OutboundMessage, SwapRecord
from app.services.seller_swap_dispatch import dispatch_swap_proposals
from app.services.wallet_service import WalletService
from tests.test_seller_swap_dispatch import scenario, _incoming, _record, _reservation, FakeWallet

PRIVATE_KEY = ec.generate_private_key(ec.SECP256R1())


def setup_module():
    assert (engine.url.database or '').startswith('energyshark_test_')
    run_migrations()


def _event(scenario, bid, *, claim_changes=None, changes=None, now=None):
    now = now or datetime.now(timezone.utc)
    swap = _record(bid['idpk'])
    swap_id = f'swp-{uuid4().hex[:12]}'
    expires_at = now + timedelta(seconds=45)
    amount = float(swap.take_amount)
    give = {'asset': 'KWH', 'amount': int(swap.give_amount)}
    take = {'asset': 'CR', 'amount': amount}
    claims = {
        'iss': 'wallet.energyshark', 'sub': swap_id,
        'iat': int(now.timestamp()), 'exp': int(expires_at.timestamp()),
        'proposer': CITY_ID, 'give': give, 'take': take, 'memo': scenario['offer'],
    }
    claims.update(claim_changes or {})
    token = jwt.encode(claims, PRIVATE_KEY, algorithm='ES256')
    event = {
        'idpk': str(uuid4()), 'msgId': str(uuid4()),
        'type': 'swap-proposed', 'sender': 'central',
        'timestamp': now.isoformat(), 'cycleId': scenario['cycle'],
        'data': {
            'idpk': bid['idpk'], 'swapId': swap_id,
            'proposer': CITY_ID, 'give': give, 'take': take,
            'memo': scenario['offer'], 'expiresAt': expires_at.isoformat(),
            'token': token,
        },
    }
    for key, value in (changes or {}).items():
        if key.startswith('envelope.'):
            event[key.removeprefix('envelope.')] = value
        elif value is None:
            event['data'].pop(key, None)
        else:
            event['data'][key] = value
    return event


def _post(event):
    response = TestClient(app).post('/internal/messages', json=event)
    assert response.status_code == 200, response.text
    return response.json()


def _messages(cycle):
    with Session(engine) as session:
        return session.exec(select(OutboundMessage).where(
            OutboundMessage.cycle_id == cycle,
            OutboundMessage.message_type == 'bid-accepted'
        )).all()


def _bid_status(key):
    with Session(engine) as session:
        return session.exec(select(MarketBid).where(MarketBid.idpk == key)).one()


def test_http_202_alone_never_creates_bid_accepted(scenario):
    bid = _incoming(scenario, quantity=3)
    wallet = FakeWallet()
    result = asyncio.run(dispatch_swap_proposals(client=wallet, limit=1))
    assert result['accepted_202'] == 1
    assert _messages(scenario['cycle']) == []
    assert _bid_status(bid['idpk']).status == 'RESERVED'


def test_real_central_swap_proposed_creates_exactly_one_directed_response(scenario):
    bid = _incoming(scenario, quantity=3)
    event = _event(scenario, bid)
    response = _post(event)
    assert response['status'] == 'ok'

    messages = _messages(scenario['cycle'])
    assert len(messages) == 1
    out = messages[0]
    assert out.exchange_name == 'energy.x'
    assert out.routing_key == f"city.{scenario['buyer']}"
    assert out.dispatch_required is True and out.status == 'PENDING'
    assert out.target_msg_id == bid['msgId']
    assert out.expires_at is not None
    assert out.payload['type'] == 'bid-accepted'
    assert out.payload['cityId'] == CITY_ID
    assert out.payload['cycleId'] == scenario['cycle']
    assert out.payload['data'] == {
        'target': bid['msgId'], 'offerId': scenario['offer'],
        'quantity': 3, 'pricePerEnergy': 1.235, 'amount': 3.71,
        'swapId': event['data']['swapId'],
        'swapToken': event['data']['token'],
        'expiresAt': event['data']['expiresAt'].replace('+00:00', 'Z'),
    }
    after = _record(bid['idpk'])
    assert after.swap_token == event['data']['token']  # data.token oficial
    assert after.status == 'PROPOSED'
    assert _bid_status(bid['idpk']).status == 'ACCEPTED'
    assert _reservation(bid['idpk']).status == 'HOLD'  # ¡no settled!


def test_after_offer_reprice_response_uses_frozen_acceptance_price(scenario):
    bid = _incoming(scenario, quantity=3)
    with Session(engine) as session:
        offer = session.exec(
            select(MarketOffer).where(
                MarketOffer.offer_id == scenario['offer']
            )
        ).one()
        offer.price_per_energy = Decimal('9.99')
        session.commit()
    _post(_event(scenario, bid))
    assert _messages(scenario['cycle'])[0].payload['data']['pricePerEnergy'] == 1.235
    assert _messages(scenario['cycle'])[0].payload['data']['amount'] == 3.71


def test_duplicate_event_and_another_envelope_do_not_duplicate_outbox(scenario):
    bid = _incoming(scenario)
    original = _event(scenario, bid)
    assert _post(original)['status'] == 'ok'
    assert _post(original)['status'] == 'duplicate'
    again = deepcopy(original)
    again['idpk'] = str(uuid4())
    again['msgId'] = str(uuid4())
    assert _post(again)['status'] == 'ok'
    assert len(_messages(scenario['cycle'])) == 1


@pytest.mark.parametrize('field', ['token', 'swapId', 'expiresAt', 'give', 'take', 'memo'])
def test_incomplete_event_does_not_accept_bid(scenario, field):
    bid = _incoming(scenario)
    event = _event(scenario, bid, changes={field: None})
    assert _post(event)['status'] == 'ok'
    assert not _messages(scenario['cycle'])
    assert _bid_status(bid['idpk']).status == 'RESERVED'


@pytest.mark.parametrize('claim_changes', [
    {'sub': 'otro-swap'}, {'iss': 'attacker'}, {'proposer': 'COR'},
    {'memo': 'otra-oferta'}, {'give': {'asset': 'KWH', 'amount': 999}},
    {'take': {'asset': 'CR', 'amount': 999}},
])
def test_inconsistent_es256_claims_do_not_notify(scenario, claim_changes):
    bid = _incoming(scenario)
    assert _post(_event(scenario, bid, claim_changes=claim_changes))['status'] == 'ok'
    assert not _messages(scenario['cycle'])
    assert _bid_status(bid['idpk']).status == 'RESERVED'


def test_expired_jwt_does_not_notify(scenario):
    bid = _incoming(scenario)
    event = _event(scenario, bid, claim_changes={'exp': 1})
    assert _post(event)['status'] == 'ok'
    assert not _messages(scenario['cycle'])


def test_event_without_central_sender_does_not_notify(scenario):
    bid = _incoming(scenario)
    event = _event(scenario, bid, changes={'envelope.sender': 'not-central'})
    assert _post(event)['status'] == 'ok'
    assert not _messages(scenario['cycle'])


def test_terminal_wallet_rejection_before_proposed_does_not_accept(scenario):
    bid = _incoming(scenario)
    with Session(engine) as session:
        WalletService().process_swap_event(session, event_type='swap-rejected',
            data={'idpk': bid['idpk'], 'swapId': 'swp-rejected',
                  'reason': 'INSUFFICIENT_ASSET'})
        session.commit()
    assert _reservation(bid['idpk']).status == 'RELEASED'
    assert _post(_event(scenario, bid))['status'] == 'ok'
    assert not _messages(scenario['cycle'])


def test_event_early_before_http202_still_queues_acceptance(scenario):
    bid = _incoming(scenario)
    event = _event(scenario, bid)
    assert _post(event)['status'] == 'ok'
    # Ya existe comunicación comercial aunque el worker no haya observado 202.
    assert len(_messages(scenario['cycle'])) == 1
    assert _record(bid['idpk']).proposal_http_accepted_at is None


def test_wallet_http_body_v3_has_expires_at_and_no_counterpart(scenario):
    bid = _incoming(scenario)
    from unittest.mock import patch
    import httpx
    from app.services.wallet_client import WalletClient
    wallet = WalletClient(base_url='https://wallet.test/v1', api_key='test-api-key')
    with patch('app.services.wallet_client.ObservedWalletAsyncClient') as mocked:
        post = mocked.return_value.__aenter__.return_value.post
        post.return_value = httpx.Response(202, json={'swapId': 'swp-test', 'status': 'accepted'})
        result = asyncio.run(dispatch_swap_proposals(client=wallet, limit=1))
    assert result['accepted_202'] == 1
    args, kwargs = post.call_args
    assert args == ('https://wallet.test/v1/swaps',)
    assert set(kwargs['json']) == {'give', 'take', 'memo', 'expiresAt'}
    assert kwargs['json']['memo'] == scenario['offer']
    assert kwargs['json']['give'] == {'asset': 'KWH', 'amount': 3.0}
    assert kwargs['json']['take'] == {'asset': 'CR', 'amount': 3.71}
    assert kwargs['json']['expiresAt'] == _record(bid['idpk']).proposal_expires_at.isoformat().replace('+00:00', 'Z')
    assert kwargs['headers']['Idempotency-Key'] == bid['idpk']
    assert not _messages(scenario['cycle'])


def test_two_concurrent_swap_proposed_envelopes_create_one_reply(scenario):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    bid = _incoming(scenario)
    first = _event(scenario, bid)
    second = deepcopy(first)
    second['idpk'] = str(uuid4())
    second['msgId'] = str(uuid4())
    barrier = Barrier(2)

    def send(evt):
        barrier.wait(timeout=20)
        return _post(evt)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(send, evt) for evt in (first, second)]
        assert all(f.result(timeout=30)['status'] == 'ok' for f in futures)
    assert len(_messages(scenario['cycle'])) == 1
    assert _bid_status(bid['idpk']).status == 'ACCEPTED'
