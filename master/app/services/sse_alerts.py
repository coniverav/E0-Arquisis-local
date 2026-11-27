"""Observe durable AD7/AD8 evidence without locking business rows."""
import asyncio
import logging
import os

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from ..database import engine

logger = logging.getLogger(__name__)

# These queries supply source row IDs, never task tokens, envelopes or errors.
_STUCK = """
    SELECT s.id, s.cycle_id FROM saga_executions s JOIN cycles c USING (cycle_id)
    WHERE s.status IN ('PENDING_START','RUNNING','WAITING')
      AND c.market_closes_at + interval '60 seconds' < now()
"""
_AGED = """
    SELECT id, cycle_id FROM outbound_messages
    WHERE dispatch_required AND status IN ('PENDING','CLAIMED','FAILED')
      AND created_at < now() - make_interval(secs => :seconds)
"""


def observe_age_alerts(outbox_seconds: int | None) -> int:
    rules = [('saga-stuck', 'SAGA', _STUCK)]
    if outbox_seconds is not None:
        if outbox_seconds < 1:
            raise ValueError("outbox_seconds must be positive")
        rules.append(('outbox-aged', 'OUTBOUND', _AGED))
    count = 0
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL statement_timeout = '5s'"))
        for family, source, query in rules:
            result = conn.execute(text(f"""
                WITH candidates AS MATERIALIZED (
                    SELECT r.* FROM ({query}) r
                    WHERE NOT EXISTS (
                        SELECT 1 FROM sse_alert_observations o
                        WHERE o.family = :family AND o.observation_key = CAST(r.id AS text)
                    ) ORDER BY r.id LIMIT 100
                ), first_seen AS (
                    INSERT INTO sse_alert_observations (family, observation_key)
                    SELECT :family, CAST(id AS text) FROM candidates ORDER BY id
                    ON CONFLICT DO NOTHING RETURNING observation_key
                )
                INSERT INTO sse_feed_pending (event_type, source, source_id, message_type, status, cycle_id)
                SELECT 'alert', :source, c.id, :family, 'OPEN', c.cycle_id
                FROM candidates c JOIN first_seen f ON f.observation_key = CAST(c.id AS text)
                ORDER BY c.id
            """), {'family': family, 'source': source, 'seconds': outbox_seconds})
            count += result.rowcount
    return count


async def run_age_observer():
    configured = os.getenv('SSE_OUTBOX_AGE_SECONDS')
    threshold = None
    if configured:
        try:
            threshold = int(configured)
            if threshold <= 0:
                raise ValueError
        except ValueError:
            threshold = None
            logger.error("Ignoring invalid SSE_OUTBOX_AGE_SECONDS")
    while True:
        try:
            await run_in_threadpool(observe_age_alerts, threshold)
        except SQLAlchemyError:
            logger.error("SSE age observation failed")
        await asyncio.sleep(15)
