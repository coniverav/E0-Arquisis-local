"""E3-054: capture committed saga transitions and deduplicate observed alerts."""
from alembic import op

revision = "e3054ssealerts"
down_revision = "e3034ssereplay"
branch_labels = None
depends_on = None


def upgrade():
    for table, constraint in (
        ("sse_feed_pending", "ck_sse_pending_source"),
        ("sse_feed_events", "ck_sse_feed_source"),
    ):
        op.drop_constraint(constraint, table, type_="check")
        op.create_check_constraint(constraint, table,
            "source IN ('INBOUND','OUTBOUND','RECONCILIATION','SAGA','MONITORING')")
    op.create_index("ix_sse_feed_source_record", "sse_feed_events", ["source", "source_id", "id"])
    op.execute("""
        CREATE TABLE sse_alert_observations (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            family text NOT NULL,
            observation_key text NOT NULL,
            UNIQUE (family, observation_key)
        )
    """)
    op.execute("""
        CREATE FUNCTION e3054_saga_transition() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'UPDATE' AND OLD.status IS NOT DISTINCT FROM NEW.status THEN
                RETURN NEW;
            END IF;
            IF NEW.status IN ('PENDING_START','RUNNING','WAITING','SETTLED','REJECTED',
                              'FAILED','COMPENSATED','TIMED_OUT') THEN
                PERFORM e3032_append_sse('saga', 'SAGA', NEW.id, 'saga-transition',
                                         NEW.status, NEW.cycle_id);
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER e3054_saga_sse AFTER INSERT OR UPDATE OF status ON saga_executions
        FOR EACH ROW EXECUTE FUNCTION e3054_saga_transition()
    """)
    # One reconciliation row is one measurement, including a confirmed divergence.
    op.execute("""
        CREATE OR REPLACE FUNCTION e3032_reconciliation_sse_trigger()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM e3032_append_sse(
                CASE WHEN NEW.status IN ('DIVERGENT','MISMATCH','ERROR') THEN 'alert' ELSE 'wallet' END,
                'RECONCILIATION', NEW.id,
                CASE WHEN NEW.status = 'DIVERGENT' THEN 'ledger-wallet-delta' ELSE 'wallet-reconciliation' END,
                CASE WHEN NEW.status IN ('DIVERGENT','MISMATCH','ERROR','MATCH','MATCHED','OK',
                                         'EXPLAINED','NO_LOCAL_BASELINE') THEN NEW.status ELSE 'OTHER' END,
                NEW.cycle_id);
            RETURN NEW;
        END $$
    """)


def downgrade():
    raise RuntimeError("Roll forward to preserve SSE history and its public cursor")
