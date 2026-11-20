"""Persistencia durable de sagas y callbacks de Step Functions.

Revision ID: e3016sagatokens
Revises: e2073directoffer
"""

from alembic import op
import sqlalchemy as sa
import sqlmodel


revision = "e3016sagatokens"
down_revision = "e2073directoffer"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "saga_executions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "idpk",
            sqlmodel.sql.sqltypes.AutoString(),
            nullable=False,
        ),
        sa.Column(
            "swap_id",
            sqlmodel.sql.sqltypes.AutoString(),
            nullable=True,
        ),
        sa.Column(
            "cycle_id",
            sqlmodel.sql.sqltypes.AutoString(),
            nullable=True,
        ),
        sa.Column(
            "role",
            sqlmodel.sql.sqltypes.AutoString(),
            nullable=False,
        ),
        sa.Column(
            "execution_arn",
            sqlmodel.sql.sqltypes.AutoString(),
            nullable=True,
        ),
        sa.Column(
            "status",
            sqlmodel.sql.sqltypes.AutoString(),
            nullable=False,
        ),
        sa.Column("timeout_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("terminal_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idpk", name="uq_saga_executions_idpk"),
        sa.UniqueConstraint("swap_id", name="uq_saga_executions_swap_id"),
        sa.UniqueConstraint(
            "execution_arn",
            name="uq_saga_executions_execution_arn",
        ),
    )
    for column in (
        "idpk",
        "swap_id",
        "cycle_id",
        "role",
        "execution_arn",
        "status",
        "timeout_at",
        "created_at",
        "updated_at",
    ):
        op.create_index(
            op.f(f"ix_saga_executions_{column}"),
            "saga_executions",
            [column],
            unique=False,
        )
    op.create_index(
        "ix_saga_executions_status_timeout_at",
        "saga_executions",
        ["status", "timeout_at"],
        unique=False,
    )

    op.create_table(
        "saga_task_tokens",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("saga_execution_id", sa.Integer(), nullable=False),
        sa.Column("task_token", sa.Text(), nullable=False),
        sa.Column("task_token_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "wait_type",
            sqlmodel.sql.sqltypes.AutoString(),
            nullable=False,
        ),
        sa.Column(
            "correlation_idpk",
            sqlmodel.sql.sqltypes.AutoString(),
            nullable=True,
        ),
        sa.Column(
            "swap_id",
            sqlmodel.sql.sqltypes.AutoString(),
            nullable=True,
        ),
        sa.Column(
            "status",
            sqlmodel.sql.sqltypes.AutoString(),
            nullable=False,
        ),
        sa.Column("timeout_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "callback_idpk",
            sqlmodel.sql.sqltypes.AutoString(),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["saga_execution_id"],
            ["saga_executions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "task_token_hash",
            name="uq_saga_task_tokens_token_hash",
        ),
    )
    for column in (
        "saga_execution_id",
        "wait_type",
        "correlation_idpk",
        "swap_id",
        "status",
        "timeout_at",
        "callback_idpk",
        "created_at",
    ):
        op.create_index(
            op.f(f"ix_saga_task_tokens_{column}"),
            "saga_task_tokens",
            [column],
            unique=False,
        )
    op.create_index(
        "ix_saga_task_tokens_saga_status",
        "saga_task_tokens",
        ["saga_execution_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_saga_task_tokens_status_correlation_idpk",
        "saga_task_tokens",
        ["status", "correlation_idpk"],
        unique=False,
    )
    op.create_index(
        "ix_saga_task_tokens_status_swap_id",
        "saga_task_tokens",
        ["status", "swap_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("saga_task_tokens")
    op.drop_table("saga_executions")
