"""Cuota compartida y pérdida de respuesta usando PostgreSQL de prueba y Wallet fake."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Barrier
import sys

import pytest
from sqlmodel import Session, select

from app.config import CITY_ID
from app.database import engine
from app.models import Cycle, LedgerEntry, OperationalRateLimit, SagaTaskToken, SwapRecord
from app.services.operational_rate_limits import reserve, block
from app.services.wallet_service import WalletService
from app.services.buyer_callback_dispatch import claim_buyer_callbacks, finish_buyer_callback
from tests.test_e3046_buyer_acceptance_db import scenario, buyer_enabled

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'infra/cdk/lambda_src/saga_task'))
from postgres_store import PostgresStore
from wallet_client import WalletClient, WalletConfig, HttpResponse
from errors import WalletPermanent
import handler
from buyer_acceptance import prepare_buyer_acceptance


def store():
    dsn = engine.url.render_as_string(hide_password=False).replace(
        'postgresql+psycopg://', 'postgresql://', 1)
    return PostgresStore(dsn, city_id=CITY_ID)


def test_global_120_limit_with_concurrent_lambdas_and_backend():
    gate = Barrier(16)
    def worker(index):
        # Cada worker usa su propio store/conexión, como instancias Lambda.
        client = store()
        gate.wait(timeout=15)
        results = []
        for _ in range(10):
            if index % 2:
                with Session(engine) as session:
                    results.append(reserve(session, 'wallet:write'))
                    session.commit()
            else:
                results.append(client.reserve('wallet:write'))
        return results
    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = [pool.submit(worker, index) for index in range(16)]
        results = [wait for future in futures for wait in future.result(timeout=45)]
    assert results.count(0) == 120
    assert sum(wait > 0 for wait in results) == 40
    with Session(engine) as session:
        bucket = session.get(OperationalRateLimit, f'{CITY_ID}:wallet:write')
        assert len(bucket.attempts) == 120
    assert store().reserve('wallet:read') == 0


@pytest.mark.parametrize('announced,expected', [(2, 2), (999, 120), (True, 120), (0, 120)])
def test_announced_limit_is_shared_and_never_exceeds_120(scenario, announced, expected):
    with Session(engine) as session:
        cycle = session.get(Cycle, scenario['cycle'])
        cycle.status_payload = {'market': {'limits': {'walletWritesPerMinute': announced}}}
        session.add(cycle)
        session.commit()
    first = store()
    for _ in range(expected):
        assert first.reserve('wallet:write') == 0
    assert store().reserve('wallet:write') > 0
    with Session(engine) as session:
        assert reserve(session, 'wallet:write') > 0
        session.commit()


def test_retry_after_survives_new_lambda_and_backend_instances():
    store().block('wallet:write', 7)
    store().block('wallet:write', 1)
    assert store().reserve('wallet:write') == 7
    with Session(engine) as session:
        assert reserve(session, 'wallet:write') == 7
        block(session, 'wallet:write', 10)
        session.commit()
    assert store().reserve('wallet:write') == 10


def test_reduced_quota_waits_until_enough_old_attempts_expire(scenario):
    client = scenario['store']
    now = scenario['clock'][0]
    for offset in (0, 10, 20):
        scenario['clock'][0] = now + timedelta(seconds=offset)
        assert client.reserve('wallet:write') == 0
    with Session(engine) as session:
        cycle = session.get(Cycle, scenario['cycle'])
        cycle.status_payload = {'market': {'limits': {'walletWritesPerMinute': 1}}}
        session.add(cycle)
        session.commit()
    scenario['clock'][0] = now + timedelta(seconds=60)
    assert client.reserve('wallet:write') == 20
    with Session(engine) as session:
        assert reserve(session, 'wallet:write', now=scenario['clock'][0]) == 20
        session.commit()
    scenario['clock'][0] = now + timedelta(seconds=80)
    assert client.reserve('wallet:write') == 0


def event_for(scenario):
    args = scenario['args']
    return dict(sagaExecutionId=args['saga_execution_id'],
                swapRecordId=args['swap_record_id'], swapId=args['swap_id'],
                idempotencyKey=args['idempotency_key'],
                correlationIdpk=args['idempotency_key'], operation='ACCEPT',
                taskToken='e3049-test-token',
                timeoutAt=(scenario['clock'][0] + timedelta(minutes=2)).isoformat())


@pytest.mark.parametrize('event_during_timeout', [True, False])
def test_lost_response_replay_and_late_duplicate_callback(scenario, event_during_timeout):
    state = scenario
    accepted = set()
    requests = []
    args = state['args']
    data = {'swapId': args['swap_id'], 'idpk': args['idempotency_key']}

    def settle():
        with Session(engine) as session:
            WalletService(client=object()).process_swap_event(
                session, event_type='swap-settled', data=data)
            session.commit()

    class RemoteWallet:
        def request(self, method, url, *, headers, body, timeout):
            key = headers['Idempotency-Key']
            requests.append(key)
            with Session(engine) as session:
                assert session.exec(select(SagaTaskToken)).one().status == 'WAITING'
            if key not in accepted:
                accepted.add(key)
                if event_during_timeout:
                    settle()
                raise TimeoutError('remote committed, response lost')
            return HttpResponse(200, {}, {'swapId': args['swap_id'], 'status': 'settled'})

    wallet = WalletClient(WalletConfig('https://wallet.invalid', 'fake'), state['store'],
                          transport=RemoteWallet(), sleep=lambda _: None)
    result = handler._callback_wallet_task('buyer-acceptance', event_for(state), object(),
                                           state['store'], wallet)
    assert result['awaitingWalletEvent'] and result['statusCode'] == 200
    assert len(accepted) == 1 and requests == [args['idempotency_key']] * 2
    if not event_during_timeout:
        with Session(engine) as session:
            record = session.get(SwapRecord, state['record_id'])
            assert record.status == 'ACCEPT_SUBMITTED'
        settle()
    settle()  # reentrega del mismo hecho
    with Session(engine) as session:
        items = claim_buyer_callbacks(session)
        session.commit()
        assert len(items) == 1 and items[0]['outcome'] == 'SETTLED'
        assert finish_buyer_callback(session, token_id=items[0]['id'],
                                     claim_token=items[0]['claimToken'], outcome='SUCCEEDED')
        session.commit()
        assert claim_buyer_callbacks(session) == []
        entries = session.exec(select(LedgerEntry).where(
            LedgerEntry.operation_type == 'SWAP_SETTLED',
            LedgerEntry.cycle_id == state['cycle'])).all()
        assert len(entries) == 1


def test_terminal_422_cannot_be_retried_by_another_invocation(scenario):
    class RemoteWallet:
        calls = 0
        def request(self, *args, **kwargs):
            self.calls += 1
            return HttpResponse(422, {}, {'reason': 'MALFORMED_REQUEST', 'code': 422})
    remote = RemoteWallet()
    wallet = WalletClient(WalletConfig('https://wallet.invalid', 'fake'), scenario['store'],
                          transport=remote)
    with pytest.raises(WalletPermanent):
        handler._callback_wallet_task('buyer-acceptance', event_for(scenario), object(),
                                      scenario['store'], wallet)
    scenario['clock'][0] += timedelta(seconds=3)
    with pytest.raises(WalletPermanent):
        prepare_buyer_acceptance(scenario['store'], **scenario['args'])
    assert remote.calls == 1


def test_callback_token_replay_preserves_original_deadline(scenario):
    class RemoteWallet:
        def accept(self, **kwargs):
            from wallet_client import WalletResult
            return WalletResult(202, {}, True, kwargs['idempotency_key'])
    event = event_for(scenario)
    handler._callback_wallet_task('buyer-acceptance', event, object(), scenario['store'], RemoteWallet())
    with Session(engine) as session:
        original = session.exec(select(SagaTaskToken)).one().timeout_at
    scenario['clock'][0] += timedelta(seconds=3)
    result = handler._callback_wallet_task('buyer-acceptance', event, object(), scenario['store'], RemoteWallet())
    assert result['requestSkipped'] == 'HTTP_ALREADY_ACCEPTED'
    with Session(engine) as session:
        assert session.exec(select(SagaTaskToken)).one().timeout_at == original
