"""Precio directo y tope de cantidad por versión de estrategia RF04.

Revision ID: e2073directoffer
Revises: e2072feedgap
"""
from alembic import op
import sqlalchemy as sa

revision = "e2073directoffer"
down_revision = "e2072feedgap"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("strategy_configs", sa.Column("offer_price_override", sa.Numeric(20, 4), nullable=True))
    op.add_column("strategy_configs", sa.Column("offer_quantity_cap", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("strategy_configs", "offer_quantity_cap")
    op.drop_column("strategy_configs", "offer_price_override")
