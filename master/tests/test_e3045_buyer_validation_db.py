"""E3-045: prueba de verificación del JWT desde PostgreSQL real (sin AWS)."""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
from cryptography.hazmat.primitives.asymmetric import ec
import pytest
from sqlmodel import Session, select

from app.config import CITY_ID
from app.database import engine
from app.models import MarketBid, SagaExecution, SwapRecord, OutboundMessage
from tests.test_e3044_buyer_saga_db import _incoming, _post, buyer_enabled
from tests.test_swap_accept_e2041 import PRIVATE_KEY, JWKS

LAMBDA_SRC = Path(__file__).resolve().parents[2]/'infra'/'cdk'/'lambda_src'/'saga_task'
sys.path.insert(0, str(LAMBDA_SRC))
from postgres_store import PostgresStore  # noqa: E402
from buyer_validation import (  # noqa: E402
    validate_buyer_token, BuyerExpiredToken, BuyerClaimsMismatch,
    BuyerInvalidToken, BuyerJwksUnavailable,
)

JWKS_URL = 'https://wallet.test/.well-known/jwks.json'


@pytest.fixture
def buyer_case(buyer_enabled):
    cycle, offer, bid, message = _incoming()
    expires = datetime.now(timezone.utc)+timedelta(minutes=2)
    now = datetime.now(timezone.utc)
    message['data']['expiresAt'] = expires.isoformat().replace('+00:00','Z')
    claims = {
        'iss': 'wallet.energyshark', 'sub': message['data']['swapId'],
        'iat': int(now.timestamp()), 'exp': int(expires.timestamp()),
        'proposer': 'HGW', 'give': {'asset': 'KWH', 'amount': bid['quantity']},
        'take': {'asset': 'CR', 'amount': message['data']['amount']},
        'memo': offer,
    }
    token = jwt.encode(claims, PRIVATE_KEY, algorithm='ES256',
                       headers={'kid': 'wallet-test-kid'})
    message['data']['swapToken'] = token
    assert _post(message)['status'] == 'ok'
    with Session(engine) as session:
        record = session.exec(select(SwapRecord).where(SwapRecord.idpk == message['idpk'])).one()
        start = session.exec(select(OutboundMessage).where(
            OutboundMessage.idpk == message['idpk'],
            OutboundMessage.message_type == 'SAGA_START',
        )).one()
        execution = json.loads(start.execution_input)
        assert token not in start.execution_input
        record_id = record.id
    store = PostgresStore(
        engine.url.render_as_string(hide_password=False).replace('postgresql+psycopg://','postgresql://',1),
        city_id=CITY_ID,
    )
    return cycle, bid, message, record_id, execution, store


def _validate(case, *, store=None, fetch=None):
    cycle, bid, message, record_id, execution, original_store = case
    return validate_buyer_token(
        store or original_store,
        saga_execution_id=message['idpk'], swap_record_id=record_id,
        swap_id=message['data']['swapId'], expected=execution['expectedClaims'],
        jwks_url=JWKS_URL, issuer='wallet.energyshark',
        fetch_jwks=fetch or (lambda _: JWKS),
    )


def test_valid_persisted_token_verified_without_mutation_or_leak(buyer_case):
    cycle, bid, message, record_id, execution, store = buyer_case
    first = _validate(buyer_case)
    second = _validate(buyer_case)
    assert first == second == {'verified': True, 'swapId': message['data']['swapId'],
                               'sagaExecutionId': message['idpk']}
    assert message['data']['swapToken'] not in json.dumps(first)
    with Session(engine) as session:
        record = session.get(SwapRecord, record_id)
        assert record.status == 'SUBMITTED' and record.accept_attempts == 0
        assert session.exec(select(SagaExecution).where(SagaExecution.idpk==message['idpk'])).one().status == 'PENDING_START'


def test_tampered_signature_never_verifies(buyer_case):
    _, _, message, record_id, _, _ = buyer_case
    attacker_token = jwt.encode({
        'iss': 'wallet.energyshark', 'sub': message['data']['swapId'],
        'iat': int(datetime.now(timezone.utc).timestamp()),
        'exp': int((datetime.now(timezone.utc)+timedelta(minutes=2)).timestamp()),
        'proposer': 'HGW', 'give': {'asset':'KWH','amount':6},
        'take': {'asset':'CR','amount':72}, 'memo': message['data']['offerId'],
    }, ec.generate_private_key(ec.SECP256R1()),
       algorithm='ES256', headers={'kid': 'wallet-test-kid'})
    with Session(engine) as session:
        record = session.get(SwapRecord, record_id)
        record.swap_token = attacker_token
        session.commit()
    with pytest.raises(BuyerInvalidToken):
        _validate(buyer_case)


def test_mismatched_signed_expiry_is_rejected(buyer_case):
    _, _, _, record_id, _, _ = buyer_case
    with Session(engine) as session:
        record = session.get(SwapRecord, record_id)
        record.expires_at += timedelta(seconds=7)
        session.commit()
    with pytest.raises(BuyerClaimsMismatch):
        _validate(buyer_case)


def test_expired_token_does_not_call_jwks(buyer_case):
    _, _, message, record_id, execution, store = buyer_case
    fixed = PostgresStore(store.dsn, city_id=store.city_id,
                          now=lambda: datetime.now(timezone.utc)+timedelta(minutes=5))
    calls=[]
    with pytest.raises(BuyerExpiredToken):
        _validate(buyer_case, store=fixed, fetch=lambda url: calls.append(url))
    assert calls == []


def test_jwks_unavailable_remains_retryable(buyer_case):
    with pytest.raises(BuyerJwksUnavailable):
        _validate(buyer_case, fetch=lambda _: (_ for _ in ()).throw(OSError('network')))


def test_invalid_input_expected_claims_no_wallet(buyer_case):
    _, _, message, record_id, execution, store = buyer_case
    with pytest.raises(BuyerClaimsMismatch):
        validate_buyer_token(store,
            saga_execution_id=message['idpk'], swap_record_id=record_id,
            swap_id=message['data']['swapId'],
            expected={**execution['expectedClaims'], 'proposer': 'ATTACKER'},
            jwks_url=JWKS_URL, issuer='wallet.energyshark',
            fetch_jwks=lambda _: JWKS)
