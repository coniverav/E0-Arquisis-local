"""E3-034: revocación de acceso al feed compartida entre réplicas.

Revision ID: e3034ssereplay
Revises: e3033ssequeue
"""
from alembic import op
import sqlalchemy as sa

revision = "e3034ssereplay"
down_revision = "e3033ssequeue"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sse_feed_revocations",
        sa.Column("subject", sa.Text(), primary_key=True),
    )


def downgrade() -> None:
    op.execute("LOCK TABLE sse_feed_revocations IN ACCESS EXCLUSIVE MODE")
    if op.get_bind().execute(sa.text("SELECT 1 FROM sse_feed_revocations LIMIT 1")).first():
        raise RuntimeError("Refusing to discard SSE access revocations")
    op.drop_table("sse_feed_revocations")
