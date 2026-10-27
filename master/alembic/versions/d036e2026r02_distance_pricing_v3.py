"""E2-036: conservar tarifa punitiva y ciclo de distance-table (protocolo v3).

Revision ID: d036e2026r02
Revises: c926e2026ad6
"""
from alembic import op
import sqlalchemy as sa

revision = "d036e2026r02"
down_revision = "c926e2026ad6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("distance_tables", sa.Column("cycle_id", sa.String(), nullable=True))
    op.add_column(
        "distance_tables",
        sa.Column("disabled_route_transport_cost", sa.Numeric(20, 4), nullable=True),
    )
    op.create_index(
        "ix_distance_tables_cycle_id", "distance_tables", ["cycle_id"], unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_distance_tables_cycle_id", table_name="distance_tables")
    op.drop_column("distance_tables", "disabled_route_transport_cost")
    op.drop_column("distance_tables", "cycle_id")
