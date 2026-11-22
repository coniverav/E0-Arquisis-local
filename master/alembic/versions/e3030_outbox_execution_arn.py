"""E3-030: conservar el ARN confirmado por StartExecution.

Revision ID: e3030relayarn
Revises: e3026outboxclaims
"""

from alembic import op
import sqlalchemy as sa


revision = "e3030relayarn"
down_revision = "e3026outboxclaims"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "outbound_messages",
        sa.Column("execution_arn", sa.Text(), nullable=True),
    )
    op.create_index(
        "uq_outbound_execution_arn",
        "outbound_messages",
        ["execution_arn"],
        unique=True,
        postgresql_where=sa.text("execution_arn IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_outbound_execution_arn", table_name="outbound_messages")
    op.drop_column("outbound_messages", "execution_arn")
