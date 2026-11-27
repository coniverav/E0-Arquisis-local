"""Authenticated SSE replay for the node's shared operator feed."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session
from starlette.concurrency import run_in_threadpool

from ..auth import verify_jwt
from ..database import engine
from ..services.sse_projection import BATCH_SIZE

logger = logging.getLogger(__name__)
router = APIRouter(tags=['events'])
POLL_SECONDS = 1.5
HEARTBEAT_SECONDS = 15.0
_CURSOR_RE = re.compile(r'(?:0|[1-9][0-9]{0,18})\Z')


def authorize_sse(claims: dict = Depends(verify_jwt)) -> dict[str, Any]:
    subject = claims.get('sub') if isinstance(claims, dict) else None
    if not isinstance(subject, str) or not subject.strip():
        raise HTTPException(status_code=403, detail='User identity required')
    required = os.getenv('SSE_REQUIRED_SCOPE', '').strip() or 'read:events'
    scope = claims.get('scope')
    if not isinstance(scope, str) or required not in scope.split():
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


def read_batch(after_id: int, *, subject: str) -> list[dict]:
    """Read authorization, the high-water mark and one page in one snapshot."""
    try:
        with Session(engine) as session:
            session.execute(text("SET LOCAL statement_timeout = '5s'"))
            rows = session.execute(text('''
                SELECT cursor.last_id, revocation.subject IS NOT NULL AS revoked,
                       EXISTS (SELECT 1 FROM sse_feed_events WHERE id = :after_id) AS checkpoint,
                       events.*
                FROM sse_feed_cursor AS cursor
                LEFT JOIN sse_feed_revocations AS revocation ON revocation.subject = :subject
                LEFT JOIN LATERAL (
                    SELECT id, event_type, source, message_type, status, cycle_id, occurred_at
                    FROM sse_feed_events
                    WHERE id > :after_id AND id <= cursor.last_id AND revocation.subject IS NULL
                    ORDER BY id LIMIT :batch
                ) AS events ON true
                WHERE cursor.singleton_id = 1
                ORDER BY events.id
            '''), {'after_id': after_id, 'subject': subject, 'batch': BATCH_SIZE}).mappings().all()
    except SQLAlchemyError as exc:
        logger.error('SSE replay query failed (%s)', type(exc).__name__)
        raise HTTPException(status_code=503, detail='SSE replay unavailable') from exc

    if not rows:
        logger.error('SSE cursor state is missing')
        raise HTTPException(status_code=503, detail='SSE replay unavailable')
    state = rows[0]
    if state['revoked']:
        raise HTTPException(status_code=403, detail='SSE access revoked')
    if after_id > state['last_id']:
        raise HTTPException(status_code=409, detail='SSE cursor is ahead of the server')
    events = [dict(row) for row in rows if row['id'] is not None]
    expected_count = min(BATCH_SIZE, state['last_id'] - after_id)
    if (after_id and not state['checkpoint']) or len(events) != expected_count or any(
        row['id'] != expected_id for expected_id, row in enumerate(events, start=after_id + 1)
    ):
        logger.error('SSE replay history has a gap after cursor %d', after_id)
        raise HTTPException(status_code=503, detail='SSE replay unavailable')
    return events


def public_event(row: dict) -> dict[str, Any]:
    """Positive allowlist: no JSONB, idpk, msgId, API keys, tokens or errors."""
    when = row['occurred_at']
    if isinstance(when, datetime):
        when_str = when.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
    else:
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


async def sse_messages(request: Request, *, after_id: int, subject: str, expires_at: int | float):
    """Stream confirmed events without keeping a DB transaction across frames."""
    cursor = after_id
    last_heartbeat = time.monotonic()
    while time.time() < expires_at and not await request.is_disconnected():
        try:
            rows = await run_in_threadpool(read_batch, cursor, subject=subject)
        except HTTPException:
            # Headers have already been sent. Close so the client can reconnect.
            return
        checked_at = time.monotonic()
        if time.time() >= expires_at:
            return
        if rows:
            for row in rows:
                if await request.is_disconnected() or time.time() >= expires_at:
                    return
                if time.monotonic() - checked_at >= POLL_SECONDS:
                    break  # Refresh authorization before sending more to a slow client.
                yield encode_sse(row)
                cursor = row['id']
            last_heartbeat = time.monotonic()
            continue
        if checked_at - last_heartbeat >= HEARTBEAT_SECONDS:
            yield ': heartbeat\n\n'
            last_heartbeat = checked_at
        await asyncio.sleep(min(POLL_SECONDS, max(0, expires_at - time.time())))


def valid_expiry(claims: dict) -> int | float:
    expiry = claims.get('exp')
    if type(expiry) not in (int, float) or not time.time() < expiry < float('inf'):
        raise HTTPException(401, 'Invalid or expired SSE token')
    return expiry


@router.get(
    '/events/stream',
    summary='Replay and stream operator events',
    response_class=StreamingResponse,
    responses={
        200: {'description': 'Event stream', 'content': {'text/event-stream': {}}},
        400: {'description': 'Malformed Last-Event-ID'},
        401: {'description': 'Missing, invalid or expired token'},
        403: {'description': 'Missing permission or revoked access'},
        409: {'description': 'Last-Event-ID is ahead of the published cursor'},
        503: {'description': 'Replay or authentication unavailable'},
    },
)
async def stream_events(
    request: Request,
    last_event_id: str | None = Header(None, alias='Last-Event-ID'),
    claims: dict = Depends(authorize_sse),
):
    expires_at = valid_expiry(claims)
    cursor = parse_cursor(last_event_id)
    subject = claims['sub']
    await run_in_threadpool(read_batch, cursor, subject=subject)
    if time.time() >= expires_at:
        raise HTTPException(status_code=401, detail='Invalid or expired SSE token')
    return StreamingResponse(
        sse_messages(request, after_id=cursor, subject=subject, expires_at=expires_at),
        media_type='text/event-stream',
        headers={
            'Cache-Control': 'no-cache, no-store, no-transform',
            'X-Accel-Buffering': 'no',
        },
    )


# Only constant SQL fragments are composed. Values, including idpk, stay bound.
_CORRELATION = {
    'cycleId': 'e.cycle_id = :value',
    'idpk': """
        (e.source = 'SAGA' AND e.source_id IN (SELECT id FROM saga_executions WHERE idpk = :value))
        OR (e.source = 'OUTBOUND' AND e.source_id IN (SELECT id FROM outbound_messages WHERE idpk = :value))
        OR (e.source = 'INBOUND' AND e.source_id IN (SELECT id FROM inbound_messages WHERE idpk = :value))
    """,
    'swapId': """
        (e.source = 'SAGA' AND e.source_id IN (SELECT id FROM saga_executions WHERE swap_id = :value))
        OR (e.source = 'OUTBOUND' AND e.source_id IN (SELECT id FROM outbound_messages WHERE swap_id = :value))
        OR (e.source = 'INBOUND' AND e.source_id IN (
            SELECT id FROM inbound_messages WHERE payload #>> '{data,swapId}' = :value
        ))
    """,
}


def _search_events(field: str, value: str, after: int, claims: dict) -> list[dict]:
    try:
        with Session(engine) as session:
            session.execute(text("SET LOCAL statement_timeout = '5s'"))
            rows = session.execute(text(f"""
                WITH access AS (
                    SELECT NOT EXISTS (
                        SELECT 1 FROM sse_feed_revocations WHERE subject = :subject
                    ) AS allowed
                )
                SELECT access.allowed, page.* FROM access
                LEFT JOIN LATERAL (
                    SELECT e.id, e.event_type, e.source, e.message_type, e.status, e.cycle_id, e.occurred_at
                    FROM sse_feed_events e WHERE access.allowed AND e.id > :after
                      AND ({_CORRELATION[field]})
                    ORDER BY e.id LIMIT :batch
                ) page ON true ORDER BY page.id
            """), {'subject': claims['sub'], 'after': after, 'value': value, 'batch': BATCH_SIZE}).mappings().all()
    except SQLAlchemyError:
        raise HTTPException(503, 'Event search unavailable') from None
    if not rows[0]['allowed']:
        raise HTTPException(403, 'SSE access revoked')
    valid_expiry(claims)
    return [public_event(row) for row in rows if row['id'] is not None]


@router.post(
    '/events/search', summary='Find published events by one exact correlation',
    openapi_extra={'requestBody': {'required': True, 'content': {'application/json': {'schema': {
        'type': 'object', 'additionalProperties': False,
        'oneOf': [{'required': [field]} for field in _CORRELATION],
        'properties': {**{field: {'type': 'string', 'minLength': 1, 'maxLength': 256}
                          for field in _CORRELATION}, 'after': {'type': 'string'}},
    }}}}},
)
async def search_events(request: Request, claims: dict = Depends(authorize_sse)):
    valid_expiry(claims)
    # Do not echo rejected inputs (including keys) in validation error responses.
    try:
        body = await request.json()
    except (ValueError, UnicodeError):
        raise HTTPException(400, 'Invalid search') from None
    if not isinstance(body, dict):
        raise HTTPException(400, 'Invalid search')
    after = body.pop('after', '')
    if not isinstance(after, str) or len(body) != 1:
        raise HTTPException(400, 'Invalid search')
    field, value = next(iter(body.items()))
    if field not in _CORRELATION or not isinstance(value, str) or not 0 < len(value) <= 256 or '\x00' in value:
        raise HTTPException(400, 'Invalid search')
    events = await run_in_threadpool(_search_events, field, value, parse_cursor(after), claims)
    return JSONResponse({'events': events}, headers={'Cache-Control': 'no-store'})
