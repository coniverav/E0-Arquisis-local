"""E3-046: aceptación comprador transaccional; sólo DB energyshark_test_*.

No invoca Wallet real ni AWS Step Functions.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier
from uuid import uuid4
import sys

import pytest
from sqlmodel import Session, select

from app.database import engine
from app.models import SagaTaskToken, SwapRecord
from tests.test_e3044_buyer_saga_db import _incoming, _post, buyer_enabled

LAMBDA_SOURCE = Path(__file__).resolve().parents[2] / 'infra' / 'cdk' / 'lambda_src' / 'saga_task'
sys.path.insert(0, str(LAMBDA_SOURCE))
from postgres_store import PostgresStore  # noqa: E402
from buyer_acceptance import (  # noqa: E402
    BuyerAcceptanceDeadline, BuyerAcceptanceInconsistent,
    verify_buyer_acceptance_identity, prepare_buyer_acceptance,
    complete_buyer_acceptance_attempt,
)
from wallet_client import WalletResult  # noqa: E402
import handler  # noqa: E402


@pytest.fixture
def scenario(buyer_enabled):
    assert (engine.url.database or '').startswith('energyshark_test_')
    cycle, offer, bid, incoming = _incoming()
    assert _post(incoming)['status'] == 'ok'
    with Session(engine) as session:
        record = session.exec(select(SwapRecord).where(SwapRecord.cycle_id == cycle)).one()
        record_id = record.id
    clock = [datetime.now(timezone.utc)]
    store = PostgresStore(
        engine.url.render_as_string(hide_password=False).replace('postgresql+psycopg://', 'postgresql://'),
        city_id='KLD', now=lambda: clock[0],
    )
    args = dict(saga_execution_id=incoming['idpk'], swap_record_id=record_id,
                swap_id=incoming['data']['swapId'], idempotency_key=incoming['idpk'])
    return {'store': store, 'args': args, 'clock': clock,
            'cycle': cycle, 'incoming': incoming, 'record_id': record_id}


def _record(scenario):
    with Session(engine) as session:
        return session.get(SwapRecord, scenario['record_id'])


def test_one_locked_acceptance_intent_before_network(scenario):
    store, args = scenario['store'], scenario['args']
    assert verify_buyer_acceptance_identity(store, **args)['attempts'] == 0
    assert prepare_buyer_acceptance(store, **args) == {'action': 'POST', 'attempt': 1}
    record = _record(scenario)
    assert record.status == 'ACCEPT_SUBMITTED'
    assert record.accept_attempts == 1 and record.accept_error == 'WALLET_IN_FLIGHT'
    assert record.accept_idempotency_key == args['idempotency_key']
    assert record.accept_retry_at > scenario['clock'][0]


def test_duplicate_during_in_flight_does_not_generate_post(scenario):
    store, args = scenario['store'], scenario['args']
    prepare_buyer_acceptance(store, **args)
    with pytest.raises(Exception, match='en vuelo'):
        prepare_buyer_acceptance(store, **args)
    assert _record(scenario).accept_attempts == 1


def test_202_leaves_task_pending_and_retries_do_not_post_again(scenario):
    store, args = scenario['store'], scenario['args']
    prepare_buyer_acceptance(store, **args)
    complete_buyer_acceptance_attempt(store, **args, status_code=202)
    record = _record(scenario)
    assert record.status == 'ACCEPT_SUBMITTED' and record.settled_at is None
    assert record.accept_error is None and record.accept_retry_at is None
    assert prepare_buyer_acceptance(store, **args)['action'] == 'AWAIT_EVENT'
    assert _record(scenario).accept_attempts == 1


def test_rejected_or_expired_token_cannot_start_acceptance(scenario):
    with Session(engine) as session:
        row = session.get(SwapRecord, scenario['record_id'])
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.add(row)
        session.commit()
    with pytest.raises(BuyerAcceptanceDeadline):
        prepare_buyer_acceptance(scenario['store'], **scenario['args'])
    assert _record(scenario).accept_attempts == 0


def test_bad_correlation_fails_closed_without_attempt(scenario):
    args = scenario['args']
    for changes in ({'swap_id': str(uuid4())},
                    {'idempotency_key': str(uuid4())},
                    {'swap_record_id': scenario['record_id'] + 1000}):
        with pytest.raises(BuyerAcceptanceInconsistent):
            verify_buyer_acceptance_identity(scenario['store'], **(args | changes))
    assert _record(scenario).accept_attempts == 0


def test_retry_reuses_stable_acceptance_key(scenario):
    store, args = scenario['store'], scenario['args']
    assert prepare_buyer_acceptance(store, **args)['attempt'] == 1
    complete_buyer_acceptance_attempt(store, **args, failure='WalletRetryable')
    key = _record(scenario).accept_idempotency_key
    with pytest.raises(Exception, match='backoff'):
        prepare_buyer_acceptance(store, **args)
    scenario['clock'][0] += timedelta(seconds=3)
    assert prepare_buyer_acceptance(store, **args)['attempt'] == 2
    record = _record(scenario)
    assert record.accept_attempts == 2 and record.accept_idempotency_key == key


def test_wallet_event_wins_race_over_202(scenario):
    store, args = scenario['store'], scenario['args']
    prepare_buyer_acceptance(store, **args)
    # Simula la carrera de un evento Wallet ya persistido por el backend E2.
    with Session(engine) as session:
        row = session.get(SwapRecord, scenario['record_id'])
        row.status = 'SETTLED'
        session.add(row)
        session.commit()
    result = complete_buyer_acceptance_attempt(store, **args, status_code=202)
    assert result == {'status': 'SETTLED', 'eventWonRace': True}
    assert _record(scenario).status == 'SETTLED'


def test_lambda_registers_callback_before_post_and_does_not_settle_202(scenario):
    store, args = scenario['store'], scenario['args']
    event = dict(sagaExecutionId=args['saga_execution_id'],
                 swapRecordId=args['swap_record_id'], swapId=args['swap_id'],
                 idempotencyKey=args['idempotency_key'],
                 correlationIdpk=args['idempotency_key'], operation='ACCEPT',
                 taskToken='opaque-e3046-' + str(uuid4()),
                 timeoutAt=(scenario['clock'][0] + timedelta(minutes=2)).isoformat())

    class FakeWallet:
        def __init__(self): self.calls = []
        def accept(self, *, swap_id, idempotency_key, deadline_seconds):
            self.calls.append(idempotency_key)
            with Session(engine) as session:
                tokens = session.exec(select(SagaTaskToken)).all()
                assert len(tokens) == 1 and tokens[0].status == 'WAITING'
                assert tokens[0].wait_type == 'TERMINAL'
                assert tokens[0].timeout_at <= scenario['clock'][0] + timedelta(seconds=29)
                record = session.get(SwapRecord, scenario['record_id'])
                assert record.status == 'ACCEPT_SUBMITTED'
                assert record.accept_attempts == 1
            return WalletResult(202, {'swapId': swap_id, 'status': 'accepted'}, True,
                                idempotency_key)

    wallet = FakeWallet()
    result = handler._callback_wallet_task('buyer-acceptance', event, object(), store, wallet)
    assert result['awaitingWalletEvent'] is True
    assert result['statusCode'] == 202
    assert 'wallet' not in result and 'swapToken' not in str(result)
    assert wallet.calls == [args['idempotency_key']]
    with Session(engine) as session:
        token = session.exec(select(SagaTaskToken)).one()
        assert token.status == 'WAITING'
        assert session.get(SwapRecord, scenario['record_id']).status == 'ACCEPT_SUBMITTED'


def test_two_concurrent_attempts_create_one_post_intent(scenario):
    barrier = Barrier(2)
    args, store = scenario['args'], scenario['store']

    def start():
        barrier.wait(timeout=15)
        try:
            return prepare_buyer_acceptance(store, **args)['action']
        except Exception as exc:
            return type(exc).__name__

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(start) for _ in range(2)]
        results = [future.result(timeout=20) for future in futures]
    assert results.count('POST') == 1
    assert 'WalletRetryable' in results
    assert _record(scenario).accept_attempts == 1
