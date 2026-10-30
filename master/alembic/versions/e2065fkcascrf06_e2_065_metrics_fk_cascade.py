"""E2-065: eliminar metricas dependientes al borrar un ciclo.

Revision ID: e2065fkcascrf06
Revises: e2065rf06metrics
"""
from alembic import op

revision = "e2065fkcascrf06"
down_revision = "e2065rf06metrics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "cycle_strategy_metrics_cycle_id_fkey",
        "cycle_strategy_metrics",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "cycle_strategy_metrics_cycle_id_fkey",
        "cycle_strategy_metrics", "cycles",
        ["cycle_id"], ["cycle_id"], ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint(
        "cycle_strategy_metrics_cycle_id_fkey",
        "cycle_strategy_metrics",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "cycle_strategy_metrics_cycle_id_fkey",
        "cycle_strategy_metrics", "cycles",
        ["cycle_id"], ["cycle_id"],
    )
