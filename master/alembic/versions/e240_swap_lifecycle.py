"""E2-040 a E2-044: aceptación asíncrona, trade/imbalance y conciliación.

Agrega el vínculo durable entre un `bid-accepted` y su `SwapRecord`, los
hechos `trade-recorded` / `imbalance-settlement` y las tablas de conciliación
con `GET /balance` y `GET /statement`.

Revision ID: e240swaplc
Revises: e229creditline
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
import sqlmodel


revision: str = "e240swaplc"
down_revision: Union[str, Sequence[str], None] = "e229creditline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "swap_records",
        sa.Column("source_bid_idpk", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    )
    op.add_column(
        "swap_records",
        sa.Column(
            "accept_attempts",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )
    op.add_column(
        "swap_records",
        sa.Column("accept_attempted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "swap_records",
        sa.Column("accept_error", sa.Text(), nullable=True),
    )
    op.create_index(
        op.f("ix_swap_records_source_bid_idpk"),
        "swap_records",
        ["source_bid_idpk"],
        unique=False,
    )
    op.create_unique_constraint(
        "uq_swap_records_source_bid_idpk",
        "swap_records",
        ["source_bid_idpk"],
    )

    op.create_table(
        "trade_records",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("idpk", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("msg_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("cycle_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("swap_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("offer_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("seller", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("buyer", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("role", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("counterpart_city_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("price_per_energy", sa.Numeric(precision=20, scale=4), nullable=False),
        sa.Column("energy_total", sa.Numeric(precision=20, scale=2), nullable=False),
        sa.Column("transport_per_energy", sa.Numeric(precision=20, scale=4), nullable=False),
        sa.Column("transport_total", sa.Numeric(precision=20, scale=2), nullable=False),
        sa.Column("route_enabled", sa.Boolean(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("swap_id", name="uq_trade_records_swap_id"),
    )
    for column in (
        "idpk", "msg_id", "cycle_id", "swap_id", "offer_id", "seller",
        "buyer", "role", "counterpart_city_id", "recorded_at",
    ):
        op.create_index(
            op.f(f"ix_trade_records_{column}"),
            "trade_records",
            [column],
            unique=False,
        )

    op.create_table(
        "imbalance_settlements",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("idpk", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("msg_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("cycle_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("settlement_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("kwh_held", sa.Integer(), nullable=False),
        sa.Column("deficit", sa.Integer(), nullable=False),
        sa.Column("coverage", sa.Integer(), nullable=False),
        sa.Column("price_per_energy", sa.Numeric(precision=20, scale=4), nullable=False),
        sa.Column("total", sa.Numeric(precision=20, scale=2), nullable=False),
        sa.Column("balance_after", sa.Numeric(precision=20, scale=2), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "settlement_id",
            name="uq_imbalance_settlements_settlement_id",
        ),
    )
    for column in ("idpk", "msg_id", "cycle_id", "settlement_id", "recorded_at"):
        op.create_index(
            op.f(f"ix_imbalance_settlements_{column}"),
            "imbalance_settlements",
            [column],
            unique=False,
        )

    op.create_table(
        "wallet_statement_entries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("entry_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("cycle_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("asset", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("kind", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=20, scale=4), nullable=False),
        sa.Column("counterpart", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("reference", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "entry_id",
            name="uq_wallet_statement_entries_entry_id",
        ),
    )
    for column in (
        "entry_id", "cycle_id", "asset", "kind", "counterpart",
        "reference", "created_at", "fetched_at",
    ):
        op.create_index(
            op.f(f"ix_wallet_statement_entries_{column}"),
            "wallet_statement_entries",
            [column],
            unique=False,
        )

    op.create_table(
        "wallet_reconciliations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("cycle_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("status", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("authority", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("cr_available_local", sa.Numeric(precision=20, scale=4), nullable=True),
        sa.Column("cr_available_wallet", sa.Numeric(precision=20, scale=4), nullable=False),
        sa.Column("cr_difference", sa.Numeric(precision=20, scale=4), nullable=True),
        sa.Column("cr_explained", sa.Numeric(precision=20, scale=4), nullable=True),
        sa.Column("cr_unexplained", sa.Numeric(precision=20, scale=4), nullable=True),
        sa.Column("kwh_available_local", sa.Numeric(precision=20, scale=4), nullable=True),
        sa.Column("kwh_available_wallet", sa.Numeric(precision=20, scale=4), nullable=False),
        sa.Column("kwh_difference", sa.Numeric(precision=20, scale=4), nullable=True),
        sa.Column("kwh_explained", sa.Numeric(precision=20, scale=4), nullable=True),
        sa.Column("kwh_unexplained", sa.Numeric(precision=20, scale=4), nullable=True),
        sa.Column("explanation", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("cycle_id", "status", "created_at"):
        op.create_index(
            op.f(f"ix_wallet_reconciliations_{column}"),
            "wallet_reconciliations",
            [column],
            unique=False,
        )


def downgrade() -> None:
    op.drop_table("wallet_reconciliations")
    op.drop_table("wallet_statement_entries")
    op.drop_table("imbalance_settlements")
    op.drop_table("trade_records")

    op.drop_constraint(
        "uq_swap_records_source_bid_idpk",
        "swap_records",
        type_="unique",
    )
    op.drop_index(
        op.f("ix_swap_records_source_bid_idpk"),
        table_name="swap_records",
    )
    op.drop_column("swap_records", "accept_error")
    op.drop_column("swap_records", "accept_attempted_at")
    op.drop_column("swap_records", "accept_attempts")
    op.drop_column("swap_records", "source_bid_idpk")
