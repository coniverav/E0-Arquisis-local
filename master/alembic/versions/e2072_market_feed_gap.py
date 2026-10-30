"""Señal conservadora de posible pérdida del feed tras reconexión.

Revision ID: e2072feedgap
Revises: e2070reconcile
"""
from alembic import op
import sqlalchemy as sa

revision = "e2072feedgap"
down_revision = "e2070reconcile"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("market_cycles", sa.Column("feed_gap_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("market_cycles", "feed_gap_at")
