"""E3-032/E3-033: authenticated, sanitized, durable SSE event feed.

Browser consumers use streaming fetch + Authorization: Bearer, not EventSource
with a token in a URL. Backend DB reads are bounded; no browser polling.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlmodel import Session
from starlette.concurrency import run_in_threadpool

from ..auth import verify_jwt
from ..database import engine

router = APIRouter(tags=['events'])
MAX_BATCH = 100
POLL_SECONDS = 1.5
HEARTBEAT_SECONDS = 15.0
_CURSOR_RE = re.compile(r'(?:0|[1-9][0-9]{0,18})\Z')


def authorize_sse(claims: dict = Depends(verify_jwt)) -> dict[str, Any]:
    """Match the existing authenticated operator policy for /audit/*.

    Optional Auth0 RBAC hardening: SSE_REQUIRED_SCOPE=read:events.
    Empty default preserves the current /audit/v3 identity-based access model.
    """
    if not isinstance(claims, dict) or not str(claims.get('sub') or '').strip():
        raise HTTPException(status_code=403, detail='User identity required')
    required = os.getenv('SSE_REQUIRED_SCOPE', '').strip()
    granted = str(claims.get('scope') or '').split()
    if required and required not in granted:
        raise HTTPException(status_code=403, detail='Missing SSE permission')
    return claims


def parse_cursor(raw: str | None) -> int:
    if raw is None or raw == '':
        return 0
    if not _CURSOR_RE.fullmatch(raw):
        raise HTTPException(status_code=400, detail='Invalid Last-Event-ID')
    value = int(raw)
    if value > 9_223_372_036_854_775_807:
        raise HTTPException(status_code=400, detail='Invalid Last-Event-ID')
    return value


def project_committed_events(limit: int = MAX_BATCH) -> int:
    """Publish a bounded batch of *committed* staging rows.

    The global cursor lock is acquired here, outside business transactions.
    Two projectors serialize with each other, but never block outbox writers.
    All inserts, cursor updates and staging deletes commit atomically.
    """
    with Session(engine) as session:
        with session.begin():
            cursor = int(session.execute(text(
                'SELECT last_id FROM sse_feed_cursor WHERE singleton_id = 1 FOR UPDATE'
            )).scalar_one())
            pending = session.execute(text('''
                SELECT ingest_id, event_type, source, source_id,
                       message_type, status, cycle_id, occurred_at
                FROM sse_feed_pending
                ORDER BY ingest_id
                LIMIT :limit
                FOR UPDATE
            '''), {'limit': limit}).mappings().all()
            for row in pending:
                cursor += 1
                session.execute(text('''
                    INSERT INTO sse_feed_events
                      (id, event_type, source, source_id, message_type,
                       status, cycle_id, occurred_at)
                    VALUES
                      (:id, :event_type, :source, :source_id,
                       :message_type, :status, :cycle_id, :occurred_at)
                '''), {
                    'id': cursor,
                    'event_type': row['event_type'],
                    'source': row['source'],
                    'source_id': row['source_id'],
                    'message_type': row['message_type'],
                    'status': row['status'],
                    'cycle_id': row['cycle_id'],
                    'occurred_at': row['occurred_at'],
                })
                session.execute(text(
                    'DELETE FROM sse_feed_pending WHERE ingest_id = :ingest_id'
                ), {'ingest_id': row['ingest_id']})
            if pending:
                session.execute(text('''
                    UPDATE sse_feed_cursor SET last_id = :cursor
                    WHERE singleton_id = 1
                '''), {'cursor': cursor})
        return len(pending)


def read_batch(after_id: int) -> list[dict]:
    """Short, isolated DB session per bounded fetch; no long DB transactions."""
    project_committed_events()
    with Session(engine) as session:
        result = session.execute(text('''
          SELECT id, event_type, source, message_type, status, cycle_id, occurred_at
          FROM sse_feed_events
          WHERE id > :cursor
          ORDER BY id ASC
          LIMIT :batch
        '''), {'cursor': after_id, 'batch': MAX_BATCH})
        return [dict(row._mapping) for row in result]


def last_committed_cursor() -> int:
    project_committed_events()
    with Session(engine) as session:
        return int(session.execute(text(
            'SELECT last_id FROM sse_feed_cursor WHERE singleton_id = 1'
        )).scalar_one())


def public_event(row: dict) -> dict[str, Any]:
    """Positive allowlist: no JSONB, idpk, msgId, API keys, tokens or errors."""
    when = row['occurred_at']
    if isinstance(when, datetime):
        when_str = when.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
    else:
        # The DB driver should return datetime; this is defensive only.
        raise ValueError('Unexpected SSE timestamp')
    return {
        'id': int(row['id']),
        'type': row['event_type'],
        'occurredAt': when_str,
        'cycleId': row['cycle_id'],
        'data': {
            'source': row['source'],
            'messageType': row['message_type'],
            'status': row['status'],
        },
    }


def encode_sse(row: dict) -> str:
    event = public_event(row)
    assert event['type'] in ('market', 'wallet', 'saga', 'alert')
    payload = json.dumps(event, ensure_ascii=False, separators=(',', ':'))
    return f"id: {event['id']}\nevent: {event['type']}\ndata: {payload}\n\n"


async def sse_messages(request: Request, *, after_id: int, expires_at: int | None):
    """Stream committed events and keep-alive frames, without busy looping."""
    cursor = after_id
    last_heartbeat = time.monotonic()
    try:
        while not await request.is_disconnected():
            if expires_at is not None and time.time() >= expires_at:
                return  # Force client to refresh JWT before reconnecting.
            rows = await run_in_threadpool(read_batch, cursor)
            if rows:
                for row in rows:
                    if await request.is_disconnected():
                        return
                    if expires_at is not None and time.time() >= expires_at:
                        return
                    yield encode_sse(row)
                    cursor = int(row['id'])
                last_heartbeat = time.monotonic()
                continue
            if time.monotonic() - last_heartbeat >= HEARTBEAT_SECONDS:
                yield ': heartbeat\n\n'
                last_heartbeat = time.monotonic()
            await asyncio.sleep(POLL_SECONDS)
    except asyncio.CancelledError:
        raise
    # Do not leak driver errors or internal exceptions as SSE payloads.


@router.get('/events/stream', summary='E3-033 authenticated SSE feed')
async def stream_events(
    request: Request,
    last_event_id: str | None = Header(None, alias='Last-Event-ID'),
    claims: dict = Depends(authorize_sse),
):
    cursor = parse_cursor(last_event_id)
    newest = await run_in_threadpool(last_committed_cursor)
    if cursor > newest:
        raise HTTPException(status_code=409, detail='SSE cursor is ahead of the server')
    exp_raw = claims.get('exp')
    expires_at = int(exp_raw) if isinstance(exp_raw, (int, float)) else None
    return StreamingResponse(
        sse_messages(request, after_id=cursor, expires_at=expires_at),
        media_type='text/event-stream',
        headers={
            'Cache-Control': 'no-cache, no-transform',
            'X-Accel-Buffering': 'no',
        },
    )
