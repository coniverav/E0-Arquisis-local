"""E3-032: durable, commit-ordered, sanitized SSE notifications.

Revision ID: e3032ssefeed
Revises: e3026outboxclaims

IDs are allocated while holding the singleton cursor row lock UNTIL COMMIT.
A PostgreSQL sequence alone cannot guarantee commit-order replay.
"""
from alembic import op
import sqlalchemy as sa

revision = "e3032ssefeed"
down_revision = "e3047callbacks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'sse_feed_cursor',
        sa.Column('singleton_id', sa.Integer(), primary_key=True),
        sa.Column('last_id', sa.BigInteger(), nullable=False),
        sa.CheckConstraint('singleton_id = 1', name='ck_sse_feed_singleton'),
    )
    op.execute('INSERT INTO sse_feed_cursor (singleton_id, last_id) VALUES (1, 0)')
    op.create_table(
        'sse_feed_events',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column('event_type', sa.String(16), nullable=False),
        sa.Column('source', sa.String(20), nullable=False),
        sa.Column('source_id', sa.BigInteger(), nullable=False),
        sa.Column('message_type', sa.String(48), nullable=False),
        sa.Column('status', sa.String(32), nullable=False),
        sa.Column('cycle_id', sa.String(), nullable=True),
        sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text('clock_timestamp()')),
        sa.CheckConstraint("event_type IN ('market','wallet','saga','alert')",
                           name='ck_sse_feed_category'),
        sa.CheckConstraint("source IN ('INBOUND','OUTBOUND','RECONCILIATION')",
                           name='ck_sse_feed_source'),
    )
    op.create_index('ix_sse_feed_cycle_id_id', 'sse_feed_events', ['cycle_id', 'id'])

    op.execute('''
    CREATE FUNCTION e3032_append_sse(
        p_category varchar, p_source varchar, p_source_id bigint,
        p_message_type varchar, p_status varchar, p_cycle_id varchar
    ) RETURNS void LANGUAGE plpgsql AS $body$
    DECLARE next_id bigint;
    BEGIN
        -- This UPDATE holds a row lock until the source transaction COMMITs.
        INSERT INTO sse_feed_cursor (singleton_id, last_id) VALUES (1, 0)
          ON CONFLICT (singleton_id) DO NOTHING;
        UPDATE sse_feed_cursor SET last_id = last_id + 1
          WHERE singleton_id = 1 RETURNING last_id INTO next_id;
        IF next_id IS NULL THEN
            RAISE EXCEPTION 'SSE_CURSOR_UNAVAILABLE';
        END IF;
        INSERT INTO sse_feed_events
          (id, event_type, source, source_id, message_type, status, cycle_id)
        VALUES (next_id, p_category, p_source, p_source_id,
                p_message_type, p_status, p_cycle_id);
    END;
    $body$
    ''')
    op.execute('''
    CREATE FUNCTION e3032_audit_sse_trigger()
    RETURNS trigger LANGUAGE plpgsql AS $body$
    DECLARE
        category varchar(16);
        source_name varchar(20);
        safe_message_type varchar(48);
        safe_status varchar(32);
    BEGIN
        IF TG_OP = 'UPDATE' THEN
            IF NEW.status IS NOT DISTINCT FROM OLD.status THEN
                RETURN NEW;
            END IF;
        END IF;
        IF TG_TABLE_NAME = 'inbound_messages' THEN
            source_name := 'INBOUND';
        ELSE
            source_name := 'OUTBOUND';
        END IF;

        -- Only whitelisted event classes enter the public feed.
        IF NEW.message_type = 'SAGA_START'
            AND source_name = 'OUTBOUND'
            AND to_jsonb(NEW)->>'delivery_kind' = 'STEP_FUNCTIONS' THEN
            category := 'saga';
        ELSIF NEW.status IN ('FAILED', 'NACKED', 'DISCARDED')
            OR NEW.message_type = 'nack' THEN
            category := 'alert';
        ELSIF NEW.message_type IN (
            'swap-proposed','swap-settled','swap-rejected',
            'swap-expired','swap-cancelled','transfer') THEN
            category := 'wallet';
        ELSIF NEW.message_type IN (
            'offer','offer-updated','offer-withdrawn','bid',
            'bid-accepted','bid-rejected','trade-recorded',
            'market-closed','status-statement','negotiation-report') THEN
            category := 'market';
        ELSE
            RETURN NEW;
        END IF;

        safe_message_type := CASE WHEN NEW.message_type IN (
            'SAGA_START','nack','swap-proposed','swap-settled','swap-rejected',
            'swap-expired','swap-cancelled','transfer','offer','offer-updated',
            'offer-withdrawn','bid','bid-accepted','bid-rejected',
            'trade-recorded','market-closed','status-statement',
            'negotiation-report'
        ) THEN NEW.message_type ELSE 'unknown' END;
        safe_status := CASE WHEN NEW.status IN (
            'PENDING','CLAIMED','STARTED','PUBLISHED','FAILED',
            'RECEIVED','PROCESSED','NACKED','DISCARDED','ACKED',
            'EXPIRED','CANCELLED','RETRY','CONFIRMED'
        ) THEN NEW.status ELSE 'OTHER' END;

        PERFORM e3032_append_sse(
            category, source_name, NEW.id, safe_message_type,
            safe_status, NEW.cycle_id
        );
        RETURN NEW;
    END;
    $body$
    ''')
    op.execute('''
    CREATE TRIGGER e3032_inbound_sse
      AFTER INSERT OR UPDATE OF status ON inbound_messages
      FOR EACH ROW EXECUTE FUNCTION e3032_audit_sse_trigger()
    ''')
    op.execute('''
    CREATE TRIGGER e3032_outbound_sse
      AFTER INSERT OR UPDATE OF status ON outbound_messages
      FOR EACH ROW EXECUTE FUNCTION e3032_audit_sse_trigger()
    ''')
    op.execute('''
    CREATE FUNCTION e3032_reconciliation_sse_trigger()
    RETURNS trigger LANGUAGE plpgsql AS $body$
    BEGIN
        PERFORM e3032_append_sse(
            CASE WHEN NEW.status IN ('MISMATCH','ERROR')
                 THEN 'alert' ELSE 'wallet' END,
            'RECONCILIATION', NEW.id, 'wallet-reconciliation',
            CASE WHEN NEW.status IN ('MATCH','MATCHED','OK','MISMATCH','ERROR')
                 THEN NEW.status ELSE 'OTHER' END,
            NEW.cycle_id
        );
        RETURN NEW;
    END;
    $body$
    ''')
    op.execute('''
    CREATE TRIGGER e3032_reconciliation_sse
      AFTER INSERT ON wallet_reconciliations
      FOR EACH ROW EXECUTE FUNCTION e3032_reconciliation_sse_trigger()
    ''')


def downgrade() -> None:
    if op.get_bind().execute(sa.text('SELECT 1 FROM sse_feed_events LIMIT 1')).first():
        raise RuntimeError('Refusing to destroy nonempty SSE replay history')
    op.execute('DROP TRIGGER IF EXISTS e3032_reconciliation_sse ON wallet_reconciliations')
    op.execute('DROP TRIGGER IF EXISTS e3032_outbound_sse ON outbound_messages')
    op.execute('DROP TRIGGER IF EXISTS e3032_inbound_sse ON inbound_messages')
    op.execute('DROP FUNCTION IF EXISTS e3032_reconciliation_sse_trigger()')
    op.execute('DROP FUNCTION IF EXISTS e3032_audit_sse_trigger()')
    op.execute('DROP FUNCTION IF EXISTS e3032_append_sse(varchar,varchar,bigint,varchar,varchar,varchar)')
    op.drop_index('ix_sse_feed_cycle_id_id', table_name='sse_feed_events')
    op.drop_table('sse_feed_events')
    op.drop_table('sse_feed_cursor')
