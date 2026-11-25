"""E3-032/033 fix: persist SSE candidates without blocking source transactions.

Revision ID: e3033ssequeue
Revises: e3032ssefeed

Events are inserted transactionally into sse_feed_pending without touching
sse_feed_cursor. The SSE reader later projects *committed* rows into the
public monotonically numbered feed in a short, separate transaction.

The staging sequence is NOT an SSE cursor and must never be sent to clients.
"""
from alembic import op
import sqlalchemy as sa

revision = "e3033ssequeue"
down_revision = "e3032ssefeed"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sse_feed_pending",
        sa.Column("ingest_id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("event_type", sa.String(16), nullable=False),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("source_id", sa.BigInteger(), nullable=False),
        sa.Column("message_type", sa.String(48), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("cycle_id", sa.String(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("clock_timestamp()")),
        sa.CheckConstraint("event_type IN ('market','wallet','saga','alert')",
                           name="ck_sse_pending_category"),
        sa.CheckConstraint("source IN ('INBOUND','OUTBOUND','RECONCILIATION')",
                           name="ck_sse_pending_source"),
    )

    # Keep the same signature, so the existing inbound/outbound/reconciliation
    # triggers automatically use this replacement. No rewrite of E3-032 needed.
    op.execute('''
    CREATE OR REPLACE FUNCTION e3032_append_sse(
        p_category varchar, p_source varchar, p_source_id bigint,
        p_message_type varchar, p_status varchar, p_cycle_id varchar
    ) RETURNS void LANGUAGE plpgsql AS $body$
    BEGIN
        INSERT INTO sse_feed_pending
          (event_type, source, source_id, message_type, status, cycle_id)
        VALUES
          (p_category, p_source, p_source_id,
           p_message_type, p_status, p_cycle_id);
    END;
    $body$
    ''')


def downgrade() -> None:
    # Reverting the function would reintroduce the cross-transaction cursor
    # lock/deadlock. Rollback must be planned as a separate safe migration.
    raise RuntimeError(
        "Unsafe downgrade: would restore blocking SSE triggers. "
        "Prepare a forward-compatible rollback migration instead."
    )
