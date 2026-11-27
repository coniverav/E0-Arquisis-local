from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlmodel import Session

from app.database import engine
from app.main import app
from app.models import Cycle, WalletReconciliation
from app.routers.sse_feed import encode_sse, read_batch
from app.services.outbox_store import enqueue_broker
from app.services.saga_tokens import get_or_create_saga_execution
from app.services.sse_alerts import observe_age_alerts
from app.services.sse_projection import project_committed_events
from app.workers.sse_incidents import import_incidents
from tests.test_e3032_e3033_sse import bearer
from tests.test_e3036_outbox_faults import new_envelope, persist_offer
from tests.test_e3053_sse_https import https_route

client = TestClient(app)


def saga(session, key="private-idpk", cycle="cycle", swap="private-swap"):
    execution, _ = get_or_create_saga_execution(
        session, idpk=key, cycle_id=cycle, swap_id=swap, role='SELLER',
        timeout_at=datetime.now(timezone.utc) + timedelta(hours=1))
    return execution


@pytest.mark.parametrize('terminal', ['SETTLED', 'REJECTED', 'FAILED', 'COMPENSATED', 'TIMED_OUT'])
def test_saga_capture_search_access_and_replay(bearer, terminal):
    with Session(engine) as db:
        saga(db)
        db.rollback()
    assert project_committed_events() == 0
    with engine.begin() as locked, Session(engine) as db:
        locked.execute(text("SELECT last_id FROM sse_feed_cursor FOR UPDATE"))
        db.execute(text("SET LOCAL lock_timeout = '1s'"))
        execution = saga(db)
        execution.execution_arn = 'PRIVATE_ARN'
        execution.status = 'RUNNING'
        db.flush()
        db.execute(text("UPDATE saga_executions SET status = status"))
        execution.status = 'WAITING'
        db.flush()
        execution.status = terminal
        db.commit()
    assert project_committed_events() == 4
    assert project_committed_events() == 0
    assert [row['status'] for row in read_batch(1, subject='alice')] == ['RUNNING', 'WAITING', terminal]
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO sse_feed_revocations VALUES ('bob')"))

    def search(payload, token=None):
        headers = {'Authorization': 'Bearer ' + token} if token else {}
        return client.post('/events/search', json=payload, headers=headers)

    for field, value in [('cycleId', 'cycle'), ('swapId', 'private-swap'), ('idpk', 'private-idpk')]:
        payload = {field: value, 'after': '1'}
        assert search(payload).status_code == 401
        for denied in (bearer('bob'), bearer('alice', scope='no')):
            assert search(payload, denied).status_code == 403
        response = search(payload, bearer('alice'))
        assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
        assert [e['id'] for e in response.json()['events']] == [2, 3, 4]
        assert 'private-' not in response.text and 'PRIVATE_ARN' not in response.text
    bad = search({'idpk': ['SENTINEL_SECRET']}, bearer('alice'))
    assert bad.status_code == 400 and 'SENTINEL' not in bad.text


def test_local_alerts_only_on_measured_conditions_and_once_across_replicas():
    now = datetime.now(timezone.utc)
    cycle_id, _ = persist_offer()
    with Session(engine) as db:
        cycle = db.get(Cycle, cycle_id)
        cycle.market_closes_at = now + timedelta(minutes=1)
        db.add(cycle)
        saga(db, cycle=cycle_id)
        for status in ('NO_LOCAL_BASELINE', 'EXPLAINED', 'DIVERGENT'):
            db.add(WalletReconciliation(cycle_id=cycle_id, status=status, cr_available_wallet=0,
                   kwh_available_wallet=0, created_at=now, explanation={'raw': 'SENTINEL_SECRET'}))
        db.commit()
        assert observe_age_alerts(None) == 0
        cycle.market_closes_at = now - timedelta(minutes=2)
        db.add(cycle)
        db.execute(text("UPDATE outbound_messages SET created_at = now() - interval '1 hour'"))
        db.commit()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(observe_age_alerts, 30) for _ in range(2)]
        assert sum(f.result(timeout=10) for f in futures) == 2
    assert observe_age_alerts(30) == 0
    project_committed_events()
    alerts = [r for r in read_batch(0, subject='alice') if r['event_type'] == 'alert']
    assert {r['message_type'] for r in alerts} == {'ledger-wallet-delta', 'saga-stuck', 'outbox-aged'}
    assert len(alerts) == 3 and 'SENTINEL' not in ''.join(map(encode_sse, alerts))


def test_monitoring_import_uses_condition_identity_and_deduplicates():
    def reply(request):
        query = json.loads(request.content)['variables']['query']
        assert "event = 'open'" in query and 'numeric(conditionId) IN (31,32)' in query
        rows = [{'conditionId': condition, 'incidentId': 'private-incident', 'timestamp': 1700000000123,
                 'description': 'SENTINEL_SECRET'} for condition in (31, 32)]
        return httpx.Response(200, json={'data': {'actor': {'account': {'nrql': {'results': rows}}}}})
    with httpx.Client(transport=httpx.MockTransport(reply)) as mock:
        args = (42, {31: 'wallet-slow', 32: 'consumer-lag'}, 1700000000000, 1700000001000, mock)
        assert import_incidents(*args) == 2
        assert import_incidents(*args) == 0
    assert project_committed_events() == 2
    rows = read_batch(0, subject='alice')
    assert [r['message_type'] for r in rows] == ['wallet-slow', 'consumer-lag']
    assert all(r['event_type'] == 'alert' and r['status'] == 'OPEN' for r in rows)
    output = ''.join(map(encode_sse, rows))
    assert 'private-incident' not in output and 'SENTINEL' not in output


def test_search_links_audit_and_outbox_without_copying_private_fields(bearer):
    from tests.test_e3032_e3033_sse import _inbound

    envelope = new_envelope()
    cycle_id, key = envelope['cycleId'], envelope['idpk']
    with Session(engine) as db:
        enqueue_broker(db, envelope=envelope, exchange_name='market', routing_key='market.offer.KLD',
                       swap_id='private-swap')
        db.flush()
        record = _inbound('swap-settled')
        record.cycle_id = cycle_id
        record.idpk = key
        record.payload = {'data': {'swapId': 'private-swap', 'taskToken': 'SENTINEL_SECRET'}}
        db.add(record)
        db.commit()
    project_committed_events()
    headers = {'Authorization': 'Bearer ' + bearer('operator')}
    for field, value in [('swapId', 'private-swap'), ('idpk', key), ('cycleId', cycle_id)]:
        response = client.post('/events/search', headers=headers, json={field: value})
        assert response.status_code == 200
        assert [e['data']['source'] for e in response.json()['events']] == ['OUTBOUND', 'INBOUND']
        assert key not in response.text and 'SENTINEL' not in response.text and 'private-swap' not in response.text
    assert client.post('/events/search', headers=headers, json={'idpk': 'other'}).json() == {'events': []}


def test_https_search_requires_bearer_and_excludes_request_body_from_access_log(https_route, bearer, tmp_path):
    import subprocess

    url = https_route[-1].replace('/events/stream', '/events/search')
    command = [*https_route[:-1], url, '--header', 'Content-Type: application/json',
               '--data', '{"idpk":"SENTINEL_PRIVATE_SEARCH"}', '--write-out', '\n%{http_code}']
    assert subprocess.check_output(command, text=True, timeout=7).endswith('401')
    token = bearer('alice')
    result = subprocess.check_output([*command, '--header', 'Authorization: Bearer ' + token],
                                     text=True, timeout=7)
    assert result.splitlines() == ['{"events":[]}', '200']
    log = (tmp_path / 'access.log').read_text()
    assert 'POST 200' in log and 'SENTINEL_PRIVATE_SEARCH' not in log and token not in log


def test_monitoring_rejects_partial_window_and_rolls_back_unmapped_condition():
    def run(rows):
        with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(
            200, json={'data': {'actor': {'account': {'nrql': {'results': rows}}}}}
        ))) as mock:
            return import_incidents(42, {31: 'wallet-slow'}, 10000, 20000, mock)

    row = {'conditionId': 31, 'incidentId': 'private', 'timestamp': 10001}
    with pytest.raises(ValueError, match='Incomplete'):
        run([row] * 1000)
    with pytest.raises(ValueError, match='unconfigured condition'):
        run([row, dict(row, conditionId=99, timestamp=10002)])
    assert project_committed_events() == 0
    assert run([row]) == 1
