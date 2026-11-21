"""E3-025/E3-026: outbox común RabbitMQ/Step Functions y leases del relay.

Revision ID: e3026outboxclaims
Revises: e2073directoffer

Migración ADITIVA. No toca filas legadas, que pasan a delivery_kind=BROKER.
No activa un relay ni cambia la operación del connector E2.
"""
from alembic import op
import sqlalchemy as sa

revision = "e3026outboxclaims"
down_revision = "e2073directoffer"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("outbound_messages", sa.Column(
        "delivery_kind", sa.String(length=24), nullable=False, server_default="BROKER"))
    op.add_column("outbound_messages", sa.Column("state_machine_arn", sa.Text(), nullable=True))
    op.add_column("outbound_messages", sa.Column("execution_name", sa.String(length=80), nullable=True))
    op.add_column("outbound_messages", sa.Column("execution_input", sa.Text(), nullable=True))
    op.add_column("outbound_messages", sa.Column("swap_id", sa.String(), nullable=True))
    op.add_column("outbound_messages", sa.Column("lease_owner", sa.String(), nullable=True))
    op.add_column("outbound_messages", sa.Column("claim_token", sa.String(), nullable=True))
    op.add_column("outbound_messages", sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column("outbound_messages", sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True))

    op.create_check_constraint(
        "ck_outbound_delivery_kind", "outbound_messages",
        "delivery_kind IN ('BROKER', 'STEP_FUNCTIONS')")
    op.create_check_constraint(
        "ck_outbound_step_destination", "outbound_messages",
        """delivery_kind <> 'STEP_FUNCTIONS'
            OR (state_machine_arn IS NOT NULL AND execution_name IS NOT NULL
                AND execution_name = idpk AND execution_input IS NOT NULL
                AND message_type = 'SAGA_START')""")
    op.create_index("uq_outbound_saga_execution", "outbound_messages",
                    ["state_machine_arn", "execution_name"], unique=True,
                    postgresql_where=sa.text("delivery_kind = 'STEP_FUNCTIONS'"))
    op.create_index("ix_outbound_relay_claim", "outbound_messages",
                    ["status", "available_at", "lease_until", "id"],
                    postgresql_where=sa.text("dispatch_required = true"))
    op.create_index("ix_outbound_swap_id", "outbound_messages", ["swap_id"])

    # El replay NO debe recrear el envelope, el input, el destino ni sus IDs.
    # Se permite cambiar únicamente estado/lease/intentos y campos operativos.
    op.execute("""
        CREATE FUNCTION e3026_outbound_immutability_guard()
        RETURNS trigger LANGUAGE plpgsql AS $function$
        BEGIN
          IF NEW.msg_id IS DISTINCT FROM OLD.msg_id
             OR NEW.idpk IS DISTINCT FROM OLD.idpk
             OR NEW.message_type IS DISTINCT FROM OLD.message_type
             OR NEW.cycle_id IS DISTINCT FROM OLD.cycle_id
             OR NEW.payload IS DISTINCT FROM OLD.payload
             OR NEW.exchange_name IS DISTINCT FROM OLD.exchange_name
             OR NEW.routing_key IS DISTINCT FROM OLD.routing_key
             OR NEW.delivery_kind IS DISTINCT FROM OLD.delivery_kind
             OR NEW.state_machine_arn IS DISTINCT FROM OLD.state_machine_arn
             OR NEW.execution_name IS DISTINCT FROM OLD.execution_name
             OR NEW.execution_input IS DISTINCT FROM OLD.execution_input
             OR NEW.swap_id IS DISTINCT FROM OLD.swap_id
          THEN
            RAISE EXCEPTION 'OUTBOX_IMMUTABLE_INTENT';
          END IF;
          RETURN NEW;
        END;
        $function$;
    """)
    op.execute("""
        CREATE TRIGGER trg_e3026_outbound_immutability
        BEFORE UPDATE ON outbound_messages
        FOR EACH ROW EXECUTE FUNCTION e3026_outbound_immutability_guard();
    """)


def downgrade() -> None:
    # Rollback destructivo de intenciones Step Functions no está permitido.
    result = op.get_bind().execute(sa.text(
        "SELECT 1 FROM outbound_messages WHERE delivery_kind = 'STEP_FUNCTIONS' LIMIT 1"
    )).first()
    if result is not None:
        raise RuntimeError("No se puede revertir: hay intenciones STEP_FUNCTIONS guardadas")
    op.execute("DROP TRIGGER IF EXISTS trg_e3026_outbound_immutability ON outbound_messages")
    op.execute("DROP FUNCTION IF EXISTS e3026_outbound_immutability_guard()")
    op.drop_index("ix_outbound_swap_id", table_name="outbound_messages")
    op.drop_index("ix_outbound_relay_claim", table_name="outbound_messages")
    op.drop_index("uq_outbound_saga_execution", table_name="outbound_messages")
    op.drop_constraint("ck_outbound_step_destination", "outbound_messages", type_="check")
    op.drop_constraint("ck_outbound_delivery_kind", "outbound_messages", type_="check")
    for name in (
        "completed_at", "lease_until", "claim_token", "lease_owner", "swap_id",
        "execution_input", "execution_name", "state_machine_arn", "delivery_kind",
    ):
        op.drop_column("outbound_messages", name)
