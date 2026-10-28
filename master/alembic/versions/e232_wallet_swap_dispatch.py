"""E2-032: metadatos durables de despacho de POST /swaps.

Revision ID: e232walletsend
Revises: e229creditline
"""
from alembic import op
import sqlalchemy as sa

revision = 'e232walletsend'
down_revision = 'e229creditline'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('swap_records', sa.Column('proposal_attempt_count', sa.Integer(),
                                            nullable=False, server_default='0'))
    op.add_column('swap_records', sa.Column('proposal_claim_token', sa.String(), nullable=True))
    op.add_column('swap_records', sa.Column('proposal_lease_until', sa.DateTime(timezone=True), nullable=True))
    op.add_column('swap_records', sa.Column('proposal_retry_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('swap_records', sa.Column('proposal_http_accepted_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('swap_records', sa.Column('proposal_blocked_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('swap_records', sa.Column('proposal_last_error', sa.String(), nullable=True))
    op.create_index('ix_swap_records_proposal_retry_at', 'swap_records', ['proposal_retry_at'])
    op.create_index('ix_swap_records_proposal_lease_until', 'swap_records', ['proposal_lease_until'])


def downgrade() -> None:
    op.drop_index('ix_swap_records_proposal_lease_until', table_name='swap_records')
    op.drop_index('ix_swap_records_proposal_retry_at', table_name='swap_records')
    for name in ('proposal_last_error', 'proposal_blocked_at', 'proposal_http_accepted_at',
                 'proposal_retry_at', 'proposal_lease_until', 'proposal_claim_token',
                 'proposal_attempt_count'):
        op.drop_column('swap_records', name)
