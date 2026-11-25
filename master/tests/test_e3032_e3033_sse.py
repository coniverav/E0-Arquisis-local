"""E3-032–034: SSE auth, event safety, ordering, and streaming contract.

Run with the repository's PostgreSQL test fixture (conftest.py).
"""
import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlmodel import Session
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app import auth
from app.auth import verify_jwt
from app.database import engine
from app.main import app
from app.models import InboundMessage, OutboundMessage, WalletReconciliation
from app.routers import sse_feed
from app.services import sse_projection
from app.services.outbox_store import enqueue_broker

client = TestClient(app)


def _inbound(message_type, status='PROCESSED'):
    return InboundMessage(
        msg_id=str(uuid4()), idpk=str(uuid4()), message_type=message_type,
        cycle_id='sse-cycle-01', sender='HGW', status=status,
        payload={'data': {'swapToken': 'SENTINEL_SECRET', 'apiKey': 'SENTINEL_KEY'}},
        received_at=datetime.now(timezone.utc),
    )


def _db_rows():
    sse_projection.project_committed_events()
    with Session(engine) as session:
        return session.execute(text(
            'SELECT id,event_type,source,message_type,status,cycle_id,occurred_at '
            'FROM sse_feed_events ORDER BY id'
        )).mappings().all()


def test_sse_requires_authenticated_operator():
    assert client.get('/events/stream').status_code == 401
    app.dependency_overrides[verify_jwt] = lambda: {'sub': '   '}
    try:
        assert client.get('/events/stream').status_code == 403
    finally:
        app.dependency_overrides.clear()


def test_sse_cursor_validation_and_future_rejected():
    for value in ('-1', 'xyz', '1\n2', '99999999999999999999999'):
        try:
            sse_feed.parse_cursor(value)
            assert False, f'{value!r} should be rejected'
        except Exception as exc:
            assert exc.status_code == 400
    assert sse_feed.parse_cursor(None) == 0
    assert sse_feed.parse_cursor('42') == 42


def test_events_are_durable_ordered_and_never_copy_secrets():
    with Session(engine) as session:
        session.add(_inbound('offer'))
        session.add(_inbound('swap-settled'))
        session.add(_inbound('bid', status='NACKED'))
        session.commit()
    rows = _db_rows()
    assert [r['event_type'] for r in rows] == ['market', 'wallet', 'alert']
    ids = [r['id'] for r in rows]
    assert ids == sorted(set(ids))
    assert ids == [1, 2, 3]
    serialized = '\n'.join(sse_feed.encode_sse(dict(row)) for row in rows)
    assert 'SENTINEL_' not in serialized
    assert 'swapToken' not in serialized
    assert 'idpk' not in serialized
    assert '\nevent: market\n' in serialized
    assert '\nevent: wallet\n' in serialized


def test_rollback_does_not_publish_a_ghost_event():
    with Session(engine) as session:
        session.add(_inbound('offer'))
        session.flush()
        session.rollback()
    assert _db_rows() == []
    assert sse_feed.read_batch(0, subject='operator') == []


def test_saga_and_wallet_reconciliation_events():
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        outbound = OutboundMessage(
            msg_id=str(uuid4()), idpk=str(uuid4()),
            message_type='SAGA_START', status='PENDING',
            cycle_id='sse-cycle-01', delivery_kind='STEP_FUNCTIONS',
            state_machine_arn='arn:aws:states:us-east-1:123456789012:stateMachine:demo',
            # The DB enforces execution_name == idpk.
            payload={}, created_at=now, execution_name=None,
            execution_input='{}', dispatch_required=True,
        )
        outbound.execution_name = outbound.idpk
        session.add(outbound)
        session.add(WalletReconciliation(
            cycle_id='sse-cycle-01', status='MISMATCH',
            cr_available_wallet=0, kwh_available_wallet=0, created_at=now,
        ))
        session.commit()
    assert [r['event_type'] for r in _db_rows()] == ['saga', 'alert']


def test_two_concurrent_source_transactions_have_unambiguous_ids():
    def insert_one(_):
        with Session(engine) as session:
            session.add(_inbound('offer'))
            session.commit()
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(insert_one, (1, 2)))
    assert [r['id'] for r in _db_rows()] == [1, 2]


@pytest.fixture
def operator(monkeypatch):
    monkeypatch.delenv('SSE_REQUIRED_SCOPE', raising=False)
    claims = {'sub': 'operator', 'scope': 'read:events', 'exp': int(time.time()) + 300}
    monkeypatch.setitem(app.dependency_overrides, verify_jwt, lambda: claims)
    return claims


@asynccontextmanager
async def _open_stream(*, after=None, token=None):
    incoming = asyncio.Queue()
    outgoing = asyncio.Queue()
    incoming.put_nowait({'type': 'http.request', 'body': b'', 'more_body': False})
    headers = []
    if after is not None:
        headers.append((b'last-event-id', str(after).encode()))
    if token:
        headers.append((b'authorization', f'Bearer {token}'.encode()))
    scope = {
        'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.3'},
        'method': 'GET', 'path': '/events/stream', 'query_string': b'', 'headers': headers,
    }
    task = asyncio.create_task(app(scope, incoming.get, outgoing.put))
    try:
        yield outgoing
    finally:
        incoming.put_nowait({'type': 'http.disconnect'})
        await asyncio.wait_for(task, 5)


async def _next_message(messages):
    return await asyncio.wait_for(messages.get(), 5)


async def _next_event(messages, expected_id):
    frame = (await _next_message(messages))['body'].decode()
    event_id, event_type, data, _, _ = frame.split('\n')
    assert event_id == f'id: {expected_id}'
    event = json.loads(data.removeprefix('data: '))
    assert event['id'] == expected_id
    assert event_type == f"event: {event['type']}"
    assert set(event) == {'id', 'type', 'occurredAt', 'cycleId', 'data'}
    assert set(event['data']) == {'source', 'messageType', 'status'}
    assert 'SENTINEL' not in frame
    return event


def test_http_media_type_and_safe_payload(operator):
    with Session(engine) as session:
        session.add(_inbound('offer'))
        session.commit()
    assert sse_projection.project_committed_events() == 1

    async def stream():
        async with _open_stream() as messages:
            response = await _next_message(messages)
            assert response['status'] == 200
            headers = dict(response['headers'])
            assert headers[b'content-type'].startswith(b'text/event-stream')
            assert headers[b'cache-control'].startswith(b'no-cache')
            assert b'no-store' in headers[b'cache-control']
            assert (await _next_event(messages, 1))['type'] == 'market'

    asyncio.run(stream())


def test_valid_operator_scope_and_replay_after_cursor(monkeypatch):
    monkeypatch.setenv('SSE_REQUIRED_SCOPE', 'read:events')
    try:
        sse_feed.authorize_sse({'sub': 'operator'})
        assert False, 'Missing scope must deny access'
    except Exception as exc:
        assert exc.status_code == 403
    assert sse_feed.authorize_sse({'sub': 'operator', 'scope': 'read:events'})['sub'] == 'operator'

    with Session(engine) as session:
        session.add(_inbound('offer'))
        session.add(_inbound('swap-settled'))
        session.add(_inbound('bid'))
        session.commit()
    assert sse_projection.project_committed_events() == 3
    rows = sse_feed.read_batch(1, subject='operator')
    assert [x['id'] for x in rows] == [2, 3]
    monkeypatch.delenv('SSE_REQUIRED_SCOPE')


def test_future_cursor_409_before_stream_opens(operator):
    with Session(engine) as session:
        session.add(_inbound('offer'))
        session.commit()
    assert client.get('/events/stream', headers={'Last-Event-ID': '01'}).status_code == 400
    assert client.get('/events/stream', headers={'Last-Event-ID': '1'}).status_code == 409
    assert sse_feed.read_batch(0, subject='operator') == []
    assert sse_projection.project_committed_events() == 1
    assert client.get('/events/stream', headers={'Last-Event-ID': '2'}).status_code == 409


def test_projection_does_not_block_concurrent_source_transactions():
    # An open source transaction has an uncommitted staging row. A second
    # transaction must still be able to commit and be observed immediately.
    # A one-second PG lock timeout ensures a regression FAILS, not HANGS.
    with Session(engine) as slow:
        slow.add(_inbound('offer'))
        slow.flush()  # keep uncommitted
        with Session(engine) as fast:
            fast.execute(text("SET LOCAL lock_timeout = '1000ms'"))
            fast.add(_inbound('bid'))
            fast.commit()
        visible = _db_rows()
        assert len(visible) == 1
        assert visible[0]['message_type'] == 'bid'
        slow.commit()
    visible = _db_rows()
    assert [item['id'] for item in visible] == [1, 2]
    assert [item['message_type'] for item in visible] == ['bid', 'offer']
    assert sse_feed.read_batch(1, subject='operator')[0]['message_type'] == 'offer'


def _commit_inbound(*message_types):
    with Session(engine) as session:
        session.add_all([_inbound(kind) for kind in message_types])
        session.commit()


def test_projector_skips_busy_replica_without_locking_outbox():
    with Session(engine) as projector, Session(engine) as producer:
        projector.execute(text('SELECT last_id FROM sse_feed_cursor FOR UPDATE'))
        producer.execute(text("SET LOCAL lock_timeout = '1s'"))
        enqueue_broker(
            producer,
            envelope={
                'msgId': str(uuid4()), 'idpk': str(uuid4()), 'type': 'offer',
                'cycleId': 'sse-cycle-01', 'cityId': 'KLD', 'timestamp': '2026-11-25T12:00:00Z',
                'data': {'message': 'SENTINEL_OUTBOX'},
            },
            exchange_name='market', routing_key='market.offer.KLD',
        )
        producer.commit()
        assert sse_projection.project_committed_events() == 0
        assert sse_feed.read_batch(0, subject='operator') == []
        projector.rollback()
    assert sse_projection.project_committed_events() == 1
    assert sse_projection.project_committed_events() == 0
    row, = sse_feed.read_batch(0, subject='operator')
    event = sse_feed.encode_sse(row)
    assert row['source'] == 'OUTBOUND'
    assert 'SENTINEL' not in event


def test_failed_projection_preserves_candidates_and_cursor_for_retry():
    _commit_inbound('offer', 'bid')
    with engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO sse_feed_events
                (id, event_type, source, source_id, message_type, status)
            VALUES (2, 'market', 'INBOUND', 42, 'bid', 'PROCESSED')
        """))
    with pytest.raises(IntegrityError):
        sse_projection.project_committed_events()

    with engine.begin() as connection:
        assert connection.execute(text('SELECT last_id FROM sse_feed_cursor')).scalar_one() == 0
        assert connection.execute(text('SELECT count(*) FROM sse_feed_pending')).scalar_one() == 2
        assert connection.execute(text('SELECT id FROM sse_feed_events')).scalars().all() == [2]
        connection.execute(text('DELETE FROM sse_feed_events WHERE id = 2'))
    assert sse_projection.project_committed_events() == 2
    assert sse_projection.project_committed_events() == 0
    assert [row['id'] for row in sse_feed.read_batch(0, subject='operator')] == [1, 2]


def test_overlapping_projectors_publish_disjoint_batches():
    batch = sse_projection.BATCH_SIZE
    total = 2 * batch + 5
    _commit_inbound(*(['offer'] * total))
    barrier = Barrier(2)

    def project():
        barrier.wait(timeout=5)
        return sse_projection.project_committed_events()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(project) for _ in range(2)]
        counts = [future.result(timeout=10) for future in futures]
    assert sorted(counts) in ([0, batch], [batch, batch])
    while count := sse_projection.project_committed_events():
        counts.append(count)
    assert sum(counts) == total

    first = sse_feed.read_batch(0, subject='operator')
    second = sse_feed.read_batch(first[-1]['id'], subject='operator')
    third = sse_feed.read_batch(second[-1]['id'], subject='operator')
    assert [len(first), len(second), len(third)] == [batch, batch, 5]
    assert [row['id'] for row in first + second + third] == list(range(1, total + 1))


def test_missing_history_fails_before_streaming(operator):
    _commit_inbound('offer', 'bid', 'swap-settled')
    assert sse_projection.project_committed_events() == 3
    with engine.begin() as connection:
        connection.execute(text('DELETE FROM sse_feed_events WHERE id = 2'))
    for checkpoint in ('1', '2'):
        response = client.get('/events/stream', headers={'Last-Event-ID': checkpoint})
        assert response.status_code == 503
        assert response.json() == {'detail': 'SSE replay unavailable'}


@pytest.fixture
def bearer(monkeypatch):
    monkeypatch.delenv('SSE_REQUIRED_SCOPE', raising=False)
    issuer = 'https://issuer.invalid/'
    audience = 'energyshark-api-test'
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    jwk['kid'] = 'sse-tests'
    monkeypatch.setattr(auth, 'AUTH_ISSUER', issuer)
    monkeypatch.setattr(auth, 'AUTH_AUDIENCE', audience)
    monkeypatch.setattr(auth, 'AUTH_JWKS_URL', issuer + '.well-known/jwks.json')
    monkeypatch.setattr(auth, 'fetch_jwks', lambda _: {'keys': [jwk]})

    def issue(subject, scope='read:events', expires_in=300):
        return jwt.encode(
            {'sub': subject, 'scope': scope, 'exp': int(time.time()) + expires_in,
             'iss': issuer, 'aud': audience},
            key, algorithm='RS256', headers={'kid': jwk['kid']},
        )

    return issue


def test_worker_recovers_after_restart_and_replays_to_two_consumers(monkeypatch, bearer):
    monkeypatch.setattr(sse_projection, 'POLL_SECONDS', 0.01)
    monkeypatch.setattr(sse_feed, 'POLL_SECONDS', 0.01)
    alice, bob = bearer('alice'), bearer('bob')

    async def wait_for_projection(count):
        async with asyncio.timeout(5):
            while len(sse_feed.read_batch(0, subject='alice')) != count:
                await asyncio.sleep(0.01)

    async def stream():
        async with app.router.lifespan_context(app):
            _commit_inbound('offer', 'bid')
            await wait_for_projection(2)
            async with _open_stream(token=alice) as before_cut:
                assert (await _next_message(before_cut))['status'] == 200
                await _next_event(before_cut, 1)

        _commit_inbound('swap-settled')
        assert len(sse_feed.read_batch(0, subject='alice')) == 2
        async with app.router.lifespan_context(app):
            await wait_for_projection(3)
            async with _open_stream(after=1, token=alice) as replay, _open_stream(after=2, token=bob) as live:
                assert (await _next_message(replay))['status'] == 200
                assert (await _next_message(live))['status'] == 200
                await _next_event(replay, 2)
                await _next_event(replay, 3)
                await _next_event(live, 3)
                _commit_inbound('offer-withdrawn')
                assert await _next_event(replay, 4) == await _next_event(live, 4)

    asyncio.run(stream())


def test_revoked_operator_disconnects_and_other_operator_keeps_access(monkeypatch, bearer):
    monkeypatch.setattr(sse_feed, 'POLL_SECONDS', 0.01)
    alice, bob = bearer('alice'), bearer('bob')
    _commit_inbound('offer')
    assert sse_projection.project_committed_events() == 1

    async def stream():
        async with _open_stream(token=alice) as blocked, _open_stream(token=bob) as allowed:
            assert (await _next_message(blocked))['status'] == 200
            assert (await _next_message(allowed))['status'] == 200
            await _next_event(blocked, 1)
            await _next_event(allowed, 1)

            with engine.begin() as connection:
                connection.execute(text("INSERT INTO sse_feed_revocations VALUES ('alice')"))
            _commit_inbound('swap-settled')
            assert sse_projection.project_committed_events() == 1
            assert not (await _next_message(blocked))['more_body']
            await _next_event(allowed, 2)

    asyncio.run(stream())
    response = client.get('/events/stream', headers={
        'Authorization': f'Bearer {alice}', 'Last-Event-ID': '9',
    })
    assert response.status_code == 403
    assert response.json() == {'detail': 'SSE access revoked'}


def test_signed_expired_token_and_missing_operator_scope_are_denied(bearer):
    expired = bearer('alice', expires_in=-60)
    no_access = bearer('mallory', scope='read:other')
    for token, status in ((expired, 401), (no_access, 403)):
        response = client.get('/events/stream', headers={
            'Authorization': f'Bearer {token}', 'Last-Event-ID': '9',
        })
        assert response.status_code == status
        assert response.headers['content-type'].startswith('application/json')


def test_active_stream_stops_at_token_expiration(monkeypatch, operator):
    clock = SimpleNamespace(now=operator['exp'] - 1)
    monkeypatch.setattr(sse_feed, 'time', SimpleNamespace(
        time=lambda: clock.now, monotonic=time.monotonic,
    ))
    monkeypatch.setattr(sse_feed, 'POLL_SECONDS', 0.01)
    _commit_inbound('offer')
    assert sse_projection.project_committed_events() == 1

    async def stream():
        async with _open_stream() as messages:
            assert (await _next_message(messages))['status'] == 200
            await _next_event(messages, 1)
            clock.now = operator['exp']
            _commit_inbound('bid')
            assert sse_projection.project_committed_events() == 1
            assert not (await _next_message(messages))['more_body']

    asyncio.run(stream())
    assert client.get('/events/stream', headers={'Last-Event-ID': '1'}).status_code == 401
