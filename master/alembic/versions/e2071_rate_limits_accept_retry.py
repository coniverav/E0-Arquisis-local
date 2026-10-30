"""Coordinar cupos por ciudad y reintentos de aceptación Wallet.

Revision ID: e2071ratelimit
Revises: e2065fkcascrf06
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "e2071ratelimit"
down_revision = "e2065fkcascrf06"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("swap_records", sa.Column("accept_retry_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_swap_records_accept_retry_at", "swap_records", ["accept_retry_at"])
    op.create_table(
        "operational_rate_limits",
        sa.Column("key", sa.String(), primary_key=True),
        sa.Column("attempts", JSONB(), nullable=False),
        sa.Column("blocked_until", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("operational_rate_limits")
    op.drop_index("ix_swap_records_accept_retry_at", table_name="swap_records")
    op.drop_column("swap_records", "accept_retry_at")
