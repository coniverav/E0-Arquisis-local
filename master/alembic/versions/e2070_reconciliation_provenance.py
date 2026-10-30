"""Origen y tiempo oficial de snapshots Wallet para conciliación AD5.

Revision ID: e2070reconcile
Revises: e2071ratelimit
"""
from alembic import op
import sqlalchemy as sa

revision = "e2070reconcile"
down_revision = "e2071ratelimit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("wallet_balance_snapshots", sa.Column("source_swap_id", sa.String(), nullable=True))
    op.add_column("wallet_balance_snapshots", sa.Column("wallet_as_of", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_wallet_balance_snapshots_source_swap_id", "wallet_balance_snapshots", ["source_swap_id"])
    op.create_index("ix_wallet_balance_snapshots_wallet_as_of", "wallet_balance_snapshots", ["wallet_as_of"])


def downgrade() -> None:
    op.drop_index("ix_wallet_balance_snapshots_wallet_as_of", table_name="wallet_balance_snapshots")
    op.drop_index("ix_wallet_balance_snapshots_source_swap_id", table_name="wallet_balance_snapshots")
    op.drop_column("wallet_balance_snapshots", "wallet_as_of")
    op.drop_column("wallet_balance_snapshots", "source_swap_id")
