"""Permitir creditLine desconocido en snapshots de eventos (E2-022).

`balancesAfter` (contrato v3.0.1) sólo trae `CR` y `KWH` con
`available`/`held`; no incluye `creditLine`. Un snapshot `EVENT` no puede
inventar cero, así que la columna pasa a nullable. El valor oficial se
obtiene por `GET /balance` (`REST_SYNC`).

Revision ID: e229creditline
Revises: d036e2026r02
"""
from alembic import op
import sqlalchemy as sa

revision = "e229creditline"
down_revision = "d036e2026r02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "wallet_balance_snapshots",
        "credit_line",
        existing_type=sa.Numeric(20, 4),
        nullable=True,
    )


def downgrade() -> None:
    op.execute(
        "UPDATE wallet_balance_snapshots SET credit_line = 0 "
        "WHERE credit_line IS NULL"
    )
    op.alter_column(
        "wallet_balance_snapshots",
        "credit_line",
        existing_type=sa.Numeric(20, 4),
        nullable=False,
    )
