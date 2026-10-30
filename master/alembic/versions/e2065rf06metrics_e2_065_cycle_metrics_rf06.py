"""E2-065 / RF06: métricas de estrategia, cobertura y costo por ciclo.

Revision ID: e2065rf06metrics
Revises: 989a8e8cf83c
"""
from alembic import op
import sqlalchemy as sa

revision = "e2065rf06metrics"
down_revision = '989a8e8cf83c'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cycle_strategy_metrics",
        sa.Column("cycle_id", sa.String(), sa.ForeignKey("cycles.cycle_id"), primary_key=True),
        sa.Column("strategy_version", sa.Integer(), nullable=False),
        sa.Column("span_seconds", sa.Integer(), nullable=False),
        sa.Column("selection_policy", sa.String(), nullable=False),
        sa.Column("acceptance_policy", sa.String(), nullable=False),
        sa.Column("deficit_kwh", sa.Integer(), nullable=False),
        sa.Column("bought_kwh", sa.Integer(), nullable=False),
        sa.Column("covered_kwh", sa.Integer(), nullable=False),
        sa.Column("coverage_rate", sa.Numeric(8, 4), nullable=False),
        sa.Column("rejected_bids", sa.Integer(), nullable=False),
        sa.Column("rejected_incoming_bids", sa.Integer(), nullable=False),
        sa.Column("buy_cost", sa.Numeric(20, 2), nullable=False),
        sa.Column("transport_cost", sa.Numeric(20, 2), nullable=False),
        sa.Column("settlement_cost", sa.Numeric(20, 2), nullable=True),
        sa.Column("real_cost", sa.Numeric(20, 2), nullable=True),
        sa.Column("baseline_cost", sa.Numeric(20, 2), nullable=True),
        sa.Column("baseline_source", sa.String(), nullable=True),
        sa.Column("savings_vs_baseline", sa.Numeric(20, 2), nullable=True),
        sa.Column("missing_transport_trades", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_cycle_strategy_metrics_strategy_version", "cycle_strategy_metrics", ["strategy_version"])
    op.create_index("ix_cycle_strategy_metrics_status", "cycle_strategy_metrics", ["status"])
    op.create_index("ix_cycle_strategy_metrics_updated_at", "cycle_strategy_metrics", ["updated_at"])


def downgrade() -> None:
    op.drop_index("ix_cycle_strategy_metrics_updated_at", table_name="cycle_strategy_metrics")
    op.drop_index("ix_cycle_strategy_metrics_status", table_name="cycle_strategy_metrics")
    op.drop_index("ix_cycle_strategy_metrics_strategy_version", table_name="cycle_strategy_metrics")
    op.drop_table("cycle_strategy_metrics")
