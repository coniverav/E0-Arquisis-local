"""Store the exchange with each outbound message (E2-014)."""

from alembic import op
import sqlalchemy as sa

revision = "e214outbound"
down_revision = "f210v2reports"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("outbound_messages", sa.Column(
        "exchange_name", sa.String(), nullable=False, server_default="energy.x"
    ))


def downgrade() -> None:
    market_rows = op.get_bind().execute(sa.text(
        "SELECT 1 FROM outbound_messages "
        "WHERE exchange_name <> 'energy.x' LIMIT 1"
    )).first()
    if market_rows is not None:
        raise RuntimeError("Cannot discard persisted market exchange destinations")
    op.drop_column("outbound_messages", "exchange_name")
