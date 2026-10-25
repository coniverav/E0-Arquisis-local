"""Persist market feed claims, cycle closure and offer snapshots (E2-017)."""

from alembic import op
import sqlalchemy as sa

revision = "e217marketfeed"
down_revision = "e214outbound"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("inbound_messages", sa.Column("offer_id", sa.String(), nullable=True))
    op.create_index("ix_inbound_messages_offer_id", "inbound_messages", ["offer_id"])

    op.create_table(
        "market_cycles",
        sa.Column("cycle_id", sa.String(), primary_key=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_msg_id", sa.String(), nullable=True),
    )
    op.create_table(
        "market_offers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("cycle_id", sa.String(), sa.ForeignKey("market_cycles.cycle_id"), nullable=False),
        sa.Column("city_id", sa.String(), nullable=False),
        sa.Column("offer_id", sa.String(), nullable=False),
        sa.Column("initial_quantity", sa.Integer(), nullable=True),
        sa.Column("remaining", sa.Integer(), nullable=True),
        sa.Column("price_per_energy", sa.Numeric(), nullable=True),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("uncertain", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_event_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_event_msg_id", sa.String(), nullable=False),
        sa.UniqueConstraint("cycle_id", "city_id", name="uq_market_offer_cycle_city"),
    )
    op.create_index("ix_market_offers_cycle_id", "market_offers", ["cycle_id"])
    op.create_index("ix_market_offers_city_id", "market_offers", ["city_id"])
    op.create_table(
        "market_event_claims",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sender", sa.String(), nullable=False),
        sa.Column("idpk", sa.String(), nullable=False),
        sa.Column("msg_id", sa.String(), nullable=False),
        sa.Column("fingerprint", sa.String(), nullable=False),
        sa.Column("inbound_id", sa.Integer(), sa.ForeignKey("inbound_messages.id", ondelete="RESTRICT"), nullable=False),
        sa.UniqueConstraint("sender", "idpk", name="uq_market_event_sender_idpk"),
        sa.UniqueConstraint("sender", "msg_id", name="uq_market_event_sender_msg_id"),
    )
    op.create_index("ix_market_event_claims_inbound_id", "market_event_claims", ["inbound_id"])


def downgrade() -> None:
    has_history = op.get_bind().execute(sa.text(
        "SELECT EXISTS (SELECT 1 FROM market_event_claims) "
        "OR EXISTS (SELECT 1 FROM market_offers) "
        "OR EXISTS (SELECT 1 FROM market_cycles) "
        "OR EXISTS (SELECT 1 FROM inbound_messages WHERE offer_id IS NOT NULL)"
    )).scalar()
    if has_history:
        raise RuntimeError("Cannot remove persisted market feed history or snapshots")

    op.drop_index("ix_market_event_claims_inbound_id", table_name="market_event_claims")
    op.drop_table("market_event_claims")
    op.drop_index("ix_market_offers_city_id", table_name="market_offers")
    op.drop_index("ix_market_offers_cycle_id", table_name="market_offers")
    op.drop_table("market_offers")
    op.drop_table("market_cycles")
    op.drop_index("ix_inbound_messages_offer_id", table_name="inbound_messages")
    op.drop_column("inbound_messages", "offer_id")
