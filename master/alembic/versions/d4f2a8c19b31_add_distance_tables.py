"""add distance tables

Revision ID: d4f2a8c19b31
Revises: a8f071ada8eb
Create Date: 2026-09-16

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel
from sqlalchemy.dialects import postgresql


revision: str = "d4f2a8c19b31"
down_revision: Union[str, Sequence[str], None] = "a8f071ada8eb"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:

    op.create_table(
        "distance_tables",
        sa.Column(
            "id",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "msg_id",
            sqlmodel.sql.sqltypes.AutoString(),
            nullable=False,
        ),
        sa.Column(
            "idpk",
            sqlmodel.sql.sqltypes.AutoString(),
            nullable=False,
        ),
        sa.Column(
            "source_timestamp",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "distances",
            postgresql.JSONB(
                astext_type=sa.Text()
            ),
            nullable=False,
        ),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "msg_id",
            name="uq_distance_table_msg_id",
        ),
        sa.UniqueConstraint(
            "idpk",
            name="uq_distance_table_idpk",
        ),
    )

    op.create_index(
        op.f("ix_distance_tables_msg_id"),
        "distance_tables",
        ["msg_id"],
        unique=False,
    )

    op.create_index(
        op.f("ix_distance_tables_idpk"),
        "distance_tables",
        ["idpk"],
        unique=False,
    )

    op.create_index(
        op.f("ix_distance_tables_source_timestamp"),
        "distance_tables",
        ["source_timestamp"],
        unique=False,
    )

    op.create_index(
        op.f("ix_distance_tables_received_at"),
        "distance_tables",
        ["received_at"],
        unique=False,
    )


def downgrade() -> None:

    op.drop_index(
        op.f("ix_distance_tables_received_at"),
        table_name="distance_tables",
    )

    op.drop_index(
        op.f("ix_distance_tables_source_timestamp"),
        table_name="distance_tables",
    )

    op.drop_index(
        op.f("ix_distance_tables_idpk"),
        table_name="distance_tables",
    )

    op.drop_index(
        op.f("ix_distance_tables_msg_id"),
        table_name="distance_tables",
    )

    op.drop_table("distance_tables")