"""E1 v2: versiones de reportes y plazos persistentes de envío (2026-10-07)."""
from alembic import op
import sqlalchemy as sa

revision = "f210v2reports"
down_revision = "e139a1b2c3d4"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_index("ix_negotiation_reports_cycle_id", table_name="negotiation_reports")
    op.create_index("ix_negotiation_reports_cycle_id", "negotiation_reports", ["cycle_id"], unique=False)
    op.add_column("negotiation_reports", sa.Column("status", sa.String(), nullable=False, server_default="PENDING"))
    op.add_column("negotiation_reports", sa.Column("reason", sa.String(), nullable=True))
    op.execute("UPDATE negotiation_reports SET status = 'PUBLISHED' WHERE sent_at IS NOT NULL")
    op.alter_column("negotiation_reports", "status", server_default=None)
    op.add_column("outbound_messages", sa.Column("available_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("outbound_messages", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("""UPDATE outbound_messages AS o SET available_at = c.report_window_opens_at,
                  expires_at = c.valid_until FROM cycles AS c
                  WHERE o.cycle_id = c.cycle_id AND o.message_type = 'negotiation-report'""")


def downgrade():
    # Impedir un downgrade que elimine correcciones existentes.
    duplicates = op.get_bind().execute(sa.text(
        "SELECT cycle_id FROM negotiation_reports GROUP BY cycle_id HAVING count(*) > 1 LIMIT 1"
    )).first()
    if duplicates:
        raise RuntimeError("Cannot restore one-report-per-cycle uniqueness without losing revisions")
    op.drop_column("outbound_messages", "expires_at")
    op.drop_column("outbound_messages", "available_at")
    op.drop_column("negotiation_reports", "reason")
    op.drop_column("negotiation_reports", "status")
    op.drop_index("ix_negotiation_reports_cycle_id", table_name="negotiation_reports")
    op.create_index("ix_negotiation_reports_cycle_id", "negotiation_reports", ["cycle_id"], unique=True)
