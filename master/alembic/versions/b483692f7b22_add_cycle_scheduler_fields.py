"""add cycle scheduler fields

Revision ID: b483692f7b22
Revises: 4f973b372ca6
Create Date: 2026-09-24 02:40:39.097438

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = "b483692f7b22"
down_revision: Union[str, Sequence[str], None] = "4f973b372ca6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add persistent scheduling state to cycles."""

    op.add_column(
        "cycles",
        sa.Column(
            "scheduler_state",
            sqlmodel.sql.sqltypes.AutoString(),
            server_default="PENDING",
            nullable=False,
        ),
    )

    op.add_column(
        "cycles",
        sa.Column(
            "report_window_opens_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )

    op.add_column(
        "cycles",
        sa.Column(
            "report_window_opened_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )

    op.add_column(
        "cycles",
        sa.Column(
            "closed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )

    op.create_index(
        op.f("ix_cycles_report_window_opens_at"),
        "cycles",
        ["report_window_opens_at"],
        unique=False,
    )

    op.create_index(
        op.f("ix_cycles_scheduler_state"),
        "cycles",
        ["scheduler_state"],
        unique=False,
    )

    # El default se utiliza solamente para migrar ciclos existentes.
    # Los nuevos ciclos utilizan el default definido en el modelo.
    op.alter_column(
        "cycles",
        "scheduler_state",
        server_default=None,
    )


def downgrade() -> None:
    """Remove persistent scheduling state from cycles."""

    op.drop_index(
        op.f("ix_cycles_scheduler_state"),
        table_name="cycles",
    )

    op.drop_index(
        op.f("ix_cycles_report_window_opens_at"),
        table_name="cycles",
    )

    op.drop_column("cycles", "closed_at")
    op.drop_column("cycles", "report_window_opened_at")
    op.drop_column("cycles", "report_window_opens_at")
    op.drop_column("cycles", "scheduler_state")