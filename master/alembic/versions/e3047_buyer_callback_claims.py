"""Lease de callbacks Standard: nuevo estado recuperable sin alterar E2.

Revision ID: e3047callbacks
Revises: e3mergesagarelay
"""
from alembic import op
import sqlalchemy as sa

revision = "e3047callbacks"
down_revision = "e3mergesagarelay"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("saga_task_tokens", sa.Column("claim_token", sa.String(128), nullable=True))
    op.add_column("saga_task_tokens", sa.Column("claim_until", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_saga_task_tokens_status_claim_until", "saga_task_tokens",
                    ["status", "claim_until"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_saga_task_tokens_status_claim_until", table_name="saga_task_tokens")
    op.drop_column("saga_task_tokens", "claim_until")
    op.drop_column("saga_task_tokens", "claim_token")
