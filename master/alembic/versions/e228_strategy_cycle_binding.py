
"""AD6: asociar cada ciclo con su versión de estrategia."""

from alembic import op
import sqlalchemy as sa


revision = "e228strategy"
down_revision = "e217marketfeed"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "cycles",
        sa.Column("strategy_version", sa.Integer(), nullable=True),
    )

    op.create_foreign_key(
        "fk_cycles_strategy_version",
        "cycles",
        "strategy_configs",
        ["strategy_version"],
        ["version"],
    )

    op.create_index(
        "ix_cycles_strategy_version",
        "cycles",
        ["strategy_version"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_cycles_strategy_version",
        table_name="cycles",
    )

    op.drop_constraint(
        "fk_cycles_strategy_version",
        "cycles",
        type_="foreignkey",
    )

    op.drop_column("cycles", "strategy_version")
