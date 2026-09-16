"""E0 baseline schema

Revision ID: 48ac5d6a989a
Revises:
Create Date: 2026-09-16

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "48ac5d6a989a"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Crea el esquema persistente heredado de E0."""

    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("idpk", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("type", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("meta_content", sa.Text(), nullable=True),
        sa.Column(
            "constraints",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_index(
        op.f("ix_events_idpk"),
        "events",
        ["idpk"],
        unique=True,
    )
    op.create_index(
        op.f("ix_events_received_at"),
        "events",
        ["received_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_events_type"),
        "events",
        ["type"],
        unique=False,
    )
    op.create_index(
        op.f("ix_events_valid_until"),
        "events",
        ["valid_until"],
        unique=False,
    )

    op.create_table(
        "demands",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("city", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("demand", sa.Float(), nullable=False),
        sa.Column("unit", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_index(
        op.f("ix_demands_city"),
        "demands",
        ["city"],
        unique=False,
    )
    op.create_index(
        op.f("ix_demands_demand"),
        "demands",
        ["demand"],
        unique=False,
    )
    op.create_index(
        op.f("ix_demands_event_id"),
        "demands",
        ["event_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_demands_unit"),
        "demands",
        ["unit"],
        unique=False,
    )


def downgrade() -> None:
    """Elimina el esquema persistente de E0."""

    op.drop_index(
        op.f("ix_demands_unit"),
        table_name="demands",
    )
    op.drop_index(
        op.f("ix_demands_event_id"),
        table_name="demands",
    )
    op.drop_index(
        op.f("ix_demands_demand"),
        table_name="demands",
    )
    op.drop_index(
        op.f("ix_demands_city"),
        table_name="demands",
    )
    op.drop_table("demands")

    op.drop_index(
        op.f("ix_events_valid_until"),
        table_name="events",
    )
    op.drop_index(
        op.f("ix_events_type"),
        table_name="events",
    )
    op.drop_index(
        op.f("ix_events_received_at"),
        table_name="events",
    )
    op.drop_index(
        op.f("ix_events_idpk"),
        table_name="events",
    )
    op.drop_table("events")