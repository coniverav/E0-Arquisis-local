"""Unir las ramas de persistencia de sagas y ARN confirmado del outbox.

Revision ID: e3mergesagarelay
Revises: e3016sagatokens, e3030relayarn
"""

revision = "e3mergesagarelay"
down_revision = ("e3016sagatokens", "e3030relayarn")
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Ambas revisiones previas aplican cambios independientes.
    pass


def downgrade() -> None:
    pass
