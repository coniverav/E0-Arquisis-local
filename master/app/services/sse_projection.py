"""Publish committed SSE candidates independently of stream consumers."""
import asyncio
import logging

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session
from starlette.concurrency import run_in_threadpool

from ..database import engine

logger = logging.getLogger(__name__)
BATCH_SIZE = 100
POLL_SECONDS = 1.5


def project_committed_events() -> int:
    """Move a batch from staging into the public log in one transaction."""
    with Session(engine) as session, session.begin():
        session.execute(text("SET LOCAL statement_timeout = '5s'"))
        last_id = session.execute(text("""
            SELECT last_id FROM sse_feed_cursor
            WHERE singleton_id = 1 FOR UPDATE SKIP LOCKED
        """)).scalar_one_or_none()
        if last_id is None:
            return 0  # Another replica can finish this batch.

        # Start a fresh READ COMMITTED statement after taking the cursor lock.
        # Scan pending rows on every batch so a late commit with a lower
        # ingest_id is still projected. Only the projector deletes from pending.
        count = session.execute(text("""
            WITH pending AS (
                DELETE FROM sse_feed_pending
                WHERE ingest_id IN (
                    SELECT ingest_id FROM sse_feed_pending
                    ORDER BY ingest_id LIMIT :batch
                )
                RETURNING ingest_id, event_type, source, source_id,
                          message_type, status, cycle_id, occurred_at
            ), projected AS (
                INSERT INTO sse_feed_events
                    (id, event_type, source, source_id, message_type, status, cycle_id, occurred_at)
                SELECT :last_id + row_number() OVER (ORDER BY ingest_id),
                       event_type, source, source_id, message_type, status, cycle_id, occurred_at
                FROM pending
                RETURNING id
            )
            UPDATE sse_feed_cursor
            SET last_id = (SELECT max(id) FROM projected)
            WHERE singleton_id = 1 AND EXISTS (SELECT 1 FROM projected)
            RETURNING last_id - :last_id
        """), {"batch": BATCH_SIZE, "last_id": last_id}).scalar_one_or_none()
        return count or 0


async def run_sse_projector() -> None:
    while True:
        try:
            if await run_in_threadpool(project_committed_events) == BATCH_SIZE:
                continue
        except SQLAlchemyError:
            logger.exception("SSE projection failed")
        await asyncio.sleep(POLL_SECONDS)
