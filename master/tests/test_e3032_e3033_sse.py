"""E3-032/E3-033: SSE auth, event safety, ordering, and streaming contract.

Run with the repository's PostgreSQL test fixture (conftest.py).
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlmodel import Session
from sqlalchemy import text

from app.auth import verify_jwt
from app.database import engine
from app.main import app
from app.models import InboundMessage, OutboundMessage, WalletReconciliation
from app.routers import sse_feed

client = TestClient(app)


def _inbound(message_type, status='PROCESSED'):
    return InboundMessage(
        msg_id=str(uuid4()), idpk=str(uuid4()), message_type=message_type,
        cycle_id='sse-cycle-01', sender='HGW', status=status,
        payload={'data': {'swapToken': 'SENTINEL_SECRET', 'apiKey': 'SENTINEL_KEY'}},
        received_at=datetime.now(timezone.utc),
    )


def _db_rows():
    sse_feed.project_committed_events()
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
    assert sse_feed.last_committed_cursor() == 0


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


def test_http_media_type_and_safe_payload(monkeypatch):
    sample = {
        'id': 1, 'event_type': 'market', 'source': 'INBOUND',
        'message_type': 'offer', 'status': 'PROCESSED',
        'cycle_id': 'sse-cycle-01', 'occurred_at': datetime.now(timezone.utc),
    }
    async def fake_messages(request, *, after_id, expires_at):
        assert after_id == 0
        yield sse_feed.encode_sse(sample)
    monkeypatch.setattr(sse_feed, 'sse_messages', fake_messages)
    monkeypatch.setattr(sse_feed, 'last_committed_cursor', lambda: 1)
    app.dependency_overrides[verify_jwt] = lambda: {'sub': 'operator', 'exp': 4000000000}
    try:
        response = client.get('/events/stream')
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200, response.text
    assert response.headers['content-type'].startswith('text/event-stream')
    assert response.headers['cache-control'].startswith('no-cache')
    assert 'id: 1\n' in response.text
    assert 'event: market\n' in response.text
    assert 'SENTINEL' not in response.text


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
    rows = sse_feed.read_batch(1)
    assert [x['id'] for x in rows] == [2, 3]
    monkeypatch.delenv('SSE_REQUIRED_SCOPE')


def test_future_cursor_409_before_stream_opens(monkeypatch):
    monkeypatch.setattr(sse_feed, 'last_committed_cursor', lambda: 2)
    app.dependency_overrides[verify_jwt] = lambda: {'sub': 'operator'}
    try:
        response = client.get('/events/stream', headers={'Last-Event-ID': '3'})
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 409


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
    assert sse_feed.read_batch(1)[0]['message_type'] == 'offer'
