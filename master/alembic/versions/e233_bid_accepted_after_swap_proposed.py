"""E2-033: precio congelado de bid y caducidad persistente del POST /swaps.

Revision ID: e233bidaccepted
Revises: e232walletsend
"""
from alembic import op
import sqlalchemy as sa

revision = 'e233bidaccepted'
down_revision = 'e232walletsend'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('market_bids', sa.Column(
        'accepted_price_per_energy', sa.Numeric(20, 4), nullable=True))
    op.add_column('swap_records', sa.Column(
        'proposal_expires_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('swap_records', 'proposal_expires_at')
    op.drop_column('market_bids', 'accepted_price_per_energy')
