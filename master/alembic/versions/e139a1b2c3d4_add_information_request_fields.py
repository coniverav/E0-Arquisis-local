"""add information request fields

Revision ID: e139a1b2c3d4
Revises: b483692f7b22
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


revision: str = "e139a1b2c3d4"
down_revision: Union[str, Sequence[str], None] = "b483692f7b22"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "outbound_messages",
        sa.Column(
            "dispatch_required",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )

    op.add_column(
        "outbound_messages",
        sa.Column(
            "request_ask",
            sqlmodel.sql.sqltypes.AutoString(),
            nullable=True,
        ),
    )

    op.add_column(
        "outbound_messages",
        sa.Column(
            "response_msg_id",
            sqlmodel.sql.sqltypes.AutoString(),
            nullable=True,
        ),
    )

    op.add_column(
        "outbound_messages",
        sa.Column(
            "response_idpk",
            sqlmodel.sql.sqltypes.AutoString(),
            nullable=True,
        ),
    )

    op.add_column(
        "outbound_messages",
        sa.Column(
            "resolved_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )

    op.create_index(
        op.f("ix_outbound_messages_dispatch_required"),
        "outbound_messages",
        ["dispatch_required"],
        unique=False,
    )

    op.create_index(
        op.f("ix_outbound_messages_request_ask"),
        "outbound_messages",
        ["request_ask"],
        unique=False,
    )

    op.create_index(
        op.f("ix_outbound_messages_response_msg_id"),
        "outbound_messages",
        ["response_msg_id"],
        unique=False,
    )

    op.create_index(
        op.f("ix_outbound_messages_response_idpk"),
        "outbound_messages",
        ["response_idpk"],
        unique=False,
    )

    op.alter_column(
        "outbound_messages",
        "dispatch_required",
        server_default=None,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_outbound_messages_response_idpk"),
        table_name="outbound_messages",
    )

    op.drop_index(
        op.f("ix_outbound_messages_response_msg_id"),
        table_name="outbound_messages",
    )

    op.drop_index(
        op.f("ix_outbound_messages_request_ask"),
        table_name="outbound_messages",
    )

    op.drop_index(
        op.f("ix_outbound_messages_dispatch_required"),
        table_name="outbound_messages",
    )

    op.drop_column(
        "outbound_messages",
        "resolved_at",
    )

    op.drop_column(
        "outbound_messages",
        "response_idpk",
    )

    op.drop_column(
        "outbound_messages",
        "response_msg_id",
    )

    op.drop_column(
        "outbound_messages",
        "request_ask",
    )

    op.drop_column(
        "outbound_messages",
        "dispatch_required",
    )