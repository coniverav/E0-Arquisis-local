# Define el esquema de la base de datos, es decir, las tablas events y demands

from datetime import datetime
from typing import Any, Optional
from decimal import Decimal

from sqlalchemy import Column, DateTime, ForeignKey, Integer, Text, Numeric, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, Relationship, SQLModel


class Event(SQLModel, table=True):
    """Evento recibido desde RabbitMQ, normalizado para poder filtrar eficientemente."""

    __tablename__ = "events"

    id: Optional[int] = Field(default=None, primary_key=True)
    idpk: str = Field(index=True, unique=True, nullable=False)
    type: str = Field(index=True, nullable=False)

    # Campos de packageBody que no pertenecen a una demanda individual.
    valid_until: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False, index=True)
    )
    meta_content: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    constraints: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )

    # Timestamp exigido por el enunciado; se genera al recibir el POST en master.
    received_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False, index=True)
    )

    demands: list["Demand"] = Relationship(
        back_populates="event",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )


class Demand(SQLModel, table=True):
    """Cada elemento de packageBody.demands queda consultable por city/demand/unit."""

    __tablename__ = "demands"

    id: Optional[int] = Field(default=None, primary_key=True)
    event_id: int = Field(foreign_key="events.id", index=True, nullable=False)
    city: str = Field(index=True, nullable=False)
    demand: float = Field(index=True, nullable=False)
    unit: str = Field(index=True, nullable=False)

    event: Optional[Event] = Relationship(back_populates="demands")


class Cycle(SQLModel, table=True):
    __tablename__ = "cycles"

    cycle_id: str = Field(primary_key=True)

    # status-statement
    status_msg_id: Optional[str] = Field(default=None, index=True)
    status_idpk: Optional[str] = Field(default=None, index=True)

    generation_capacity: Optional[Decimal] = Field(
        default=None,
        sa_column=Column(Numeric(20, 2), nullable=True),
    )

    consumption: Optional[Decimal] = Field(
        default=None,
        sa_column=Column(Numeric(20, 2), nullable=True),
    )

    generation_cost: Optional[Decimal] = Field(
        default=None,
        sa_column=Column(Numeric(20, 2), nullable=True),
    )

    valid_until: Optional[datetime] = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )

    scheduler_state: str = Field(
        default="PENDING",
        index=True,
        nullable=False,
    )

    report_window_opens_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=True,
            index=True,
        ),
    )

    # Campos de tiempos de mercado central v3 (E2-023)
    market_opens_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    market_last_offer_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    market_last_bid_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    market_closes_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )

    report_window_opened_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=True,
        ),
    )

    closed_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=True,
        ),
    )

    status_payload: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False),
    )

    # Estado inicial del ledger
    opening_budget_balance: Decimal = Field(
        default=Decimal("0.00"),
        sa_column=Column(Numeric(20, 2), nullable=False),
    )

    opening_energy_balance: Decimal = Field(
        default=Decimal("0.00"),
        sa_column=Column(Numeric(20, 2), nullable=False),
    )

    # Snapshot/materialización actual
    budget_balance: Decimal = Field(
        default=Decimal("0.00"),
        sa_column=Column(Numeric(20, 2), nullable=False),
    )

    energy_balance: Decimal = Field(
        default=Decimal("0.00"),
        sa_column=Column(Numeric(20, 2), nullable=False),
    )

    # Para conocer inmediatamente la última operación
    last_sequence: int = Field(default=0, nullable=False)
    last_operation_type: Optional[str] = Field(default=None)
    last_operation_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )

    created_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )


class LedgerEntry(SQLModel, table=True):
    __tablename__ = "ledger_entries"

    __table_args__ = (
        UniqueConstraint(
            "cycle_id",
            "sequence_no",
            name="uq_ledger_cycle_sequence",
        ),
        UniqueConstraint(
            "idpk",
            "operation_type",
            name="uq_ledger_idempotent_effect",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)

    cycle_id: str = Field(
        foreign_key="cycles.cycle_id",
        index=True,
        nullable=False,
    )

    sequence_no: int = Field(nullable=False)

    # Ejemplos:
    # TRANSFER_IN
    # DEMAND_STATEMENT
    # GIVE_CONFIRMED
    # TAKE_CONFIRMED
    # PAYMENT_RECEIVED
    # PAYMENT_SENT
    operation_type: str = Field(index=True, nullable=False)

    # Llave que evita aplicar dos veces el mismo efecto.
    idpk: str = Field(index=True, nullable=False)

    # Mensaje concreto que provocó el efecto.
    source_msg_id: Optional[str] = Field(default=None, index=True)

    negotiation_id: Optional[int] = Field(
        default=None,
        foreign_key="negotiations.id",
    )

    budget_delta: Decimal = Field(
        default=Decimal("0.00"),
        sa_column=Column(Numeric(20, 2), nullable=False),
    )

    energy_delta: Decimal = Field(
        default=Decimal("0.00"),
        sa_column=Column(Numeric(20, 2), nullable=False),
    )

    # Guardarlos hace que el historial sea muy fácil de explicar.
    budget_after: Decimal = Field(
        sa_column=Column(Numeric(20, 2), nullable=False),
    )

    energy_after: Decimal = Field(
        sa_column=Column(Numeric(20, 2), nullable=False),
    )

    details: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False),
    )

    applied_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )


class Negotiation(SQLModel, table=True):
    __tablename__ = "negotiations"

    id: Optional[int] = Field(default=None, primary_key=True)

    cycle_id: str = Field(
        foreign_key="cycles.cycle_id",
        index=True,
        nullable=False,
    )

    # idpk original de la operación.
    idpk: str = Field(unique=True, index=True, nullable=False)

    latest_msg_id: Optional[str] = Field(default=None)

    direction: str = Field(nullable=False)  # give / take

    requested_quantity: Decimal = Field(
        sa_column=Column(Numeric(20, 2), nullable=False),
    )

    offered_price: Decimal = Field(
        sa_column=Column(Numeric(20, 2), nullable=False),
    )

    status: str = Field(index=True, nullable=False)

    confirmed_energy: Optional[Decimal] = Field(
        default=None,
        sa_column=Column(Numeric(20, 2), nullable=True),
    )

    confirmed_price: Optional[Decimal] = Field(
        default=None,
        sa_column=Column(Numeric(20, 2), nullable=True),
    )

    payment_quantity: Optional[Decimal] = Field(
        default=None,
        sa_column=Column(Numeric(20, 2), nullable=True),
    )

    deadline_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )

    created_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )

    updated_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )


class NegotiationReport(SQLModel, table=True):
    __tablename__ = "negotiation_reports"

    id: Optional[int] = Field(default=None, primary_key=True)

    cycle_id: str = Field(
        foreign_key="cycles.cycle_id",
        index=True,
        nullable=False,
    )

    msg_id: str = Field(unique=True, nullable=False)
    idpk: str = Field(unique=True, nullable=False)

    # Publicación no equivale a aceptación por la central.
    status: str = Field(default="PENDING", nullable=False)
    reason: Optional[str] = Field(default=None)

    budget_balance: Decimal = Field(
        sa_column=Column(Numeric(20, 2), nullable=False),
    )

    energy_balance: Decimal = Field(
        sa_column=Column(Numeric(20, 2), nullable=False),
    )

    payload: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False),
    )

    created_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )

    sent_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )

# Error de negocio recibido desde la central mediante el protocolo v2.
class ProtocolError(SQLModel, table=True):
    __tablename__ = "protocol_errors"

    id: Optional[int] = Field(default=None, primary_key=True)

    idpk: str = Field(index=True, nullable=False)
    msg_id: str = Field(index=True, unique=True, nullable=False)

    cycle_id: str = Field(index=True, nullable=False)

    reason: str = Field(index=True, nullable=False)
    code: int = Field(nullable=False)

    target_msg_id: str = Field(index=True, nullable=False)

    message: str = Field(sa_column=Column(Text, nullable=False))

    cap: Optional[float] = Field(default=None, nullable=True)
    spare: Optional[float] = Field(default=None, nullable=True)

    raw: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False),
    )

    timestamp: datetime = Field(
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            index=True,
        )
    )

    received_at: datetime = Field(
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            index=True,
        )
    )

class DistanceTable(SQLModel, table=True):
    """
    Versiones recibidas de la distance-table.

    Se conservan las versiones recibidas y la vigente se determina
    utilizando el timestamp del mensaje.
    """

    __tablename__ = "distance_tables"

    __table_args__ = (
        UniqueConstraint(
            "msg_id",
            name="uq_distance_table_msg_id",
        ),
        UniqueConstraint(
            "idpk",
            name="uq_distance_table_idpk",
        ),
    )

    id: Optional[int] = Field(
        default=None,
        primary_key=True,
    )

    msg_id: str = Field(
        index=True,
        nullable=False,
    )

    idpk: str = Field(
        index=True,
        nullable=False,
    )

    source_timestamp: datetime = Field(
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            index=True,
        )
    )

    distances: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(
            JSONB,
            nullable=False,
        ),
    )

    received_at: datetime = Field(
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            index=True,
        )
    )

#Registro canónico de idpk cuya operación fue procesada exitosamente.
#La PK global garantiza que un mismo idpk no pueda ser reclamado simultáneamente por dos réplicas de master.
class ProcessedIdpk(SQLModel, table=True):
    __tablename__ = "processed_idpks"

    idpk: str = Field(primary_key=True)

    msg_id: str = Field(index=True, nullable=False)
    message_type: str = Field(index=True, nullable=False)
    cycle_id: Optional[str] = Field(default=None, index=True)

    processed_at: datetime = Field(
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            index=True,
        )
    )

class InboundMessage(SQLModel, table=True):
    """
    Evidencia durable de cada recepción de un mensaje del protocolo.

    Un msgId NO es único en esta tabla porque una redelivery debe poder
    registrarse como una nueva recepción para efectos de auditoría.
    """

    __tablename__ = "inbound_messages"

    id: Optional[int] = Field(default=None, primary_key=True)

    msg_id: Optional[str] = Field(default=None, index=True)
    idpk: Optional[str] = Field(default=None, index=True)

    message_type: Optional[str] = Field(default=None, index=True)
    cycle_id: Optional[str] = Field(default=None, index=True)
    offer_id: Optional[str] = Field(default=None, index=True)

    payload: Optional[dict[str, Any]] = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
    )

    raw_payload: Optional[str] = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
    )

    status: str = Field(index=True, nullable=False)

    reason_code: Optional[str] = Field(default=None, index=True)

    reason: Optional[str] = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
    )

    sender: Optional[str] = Field(default=None)

    received_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False, index=True)
    )

    processed_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )


class MarketCycle(SQLModel, table=True):
    """Estado terminal del feed para un ciclo, independiente del ledger E1."""

    __tablename__ = "market_cycles"

    cycle_id: str = Field(primary_key=True)
    closed_at: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    closed_msg_id: Optional[str] = Field(default=None)


class MarketFeedOffer(SQLModel, table=True):
    """Snapshot conservador de la única oferta de una ciudad en un ciclo."""

    __tablename__ = "market_feed_offers"
    __table_args__ = (UniqueConstraint("cycle_id", "city_id", name="uq_market_offer_cycle_city"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    cycle_id: str = Field(foreign_key="market_cycles.cycle_id", index=True, nullable=False)
    city_id: str = Field(index=True, nullable=False)
    offer_id: str = Field(nullable=False)
    initial_quantity: Optional[int] = Field(default=None)
    remaining: Optional[int] = Field(default=None)
    price_per_energy: Optional[Decimal] = Field(
        default=None, sa_column=Column(Numeric(), nullable=True)
    )
    state: str = Field(nullable=False)  # ACTIVE / UNRESOLVED / WITHDRAWN / CLOSED
    uncertain: bool = Field(default=False, nullable=False)
    last_event_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    last_event_msg_id: str = Field(nullable=False)


class MarketEventClaim(SQLModel, table=True):
    """Unicidad transaccional de idpk/msgId del feed entre réplicas de master."""

    __tablename__ = "market_event_claims"
    __table_args__ = (
        UniqueConstraint("sender", "idpk", name="uq_market_event_sender_idpk"),
        UniqueConstraint("sender", "msg_id", name="uq_market_event_sender_msg_id"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    sender: str = Field(nullable=False)
    idpk: str = Field(nullable=False)
    msg_id: str = Field(nullable=False)
    fingerprint: str = Field(nullable=False)
    inbound_id: int = Field(sa_column=Column(
        Integer, ForeignKey("inbound_messages.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    ))


class OutboundMessage(SQLModel, table=True):
    """
    Evidencia e intención durable de publicaciones hacia RabbitMQ.

    Esta tabla sirve como base para el mecanismo outbox definido en AD1.
    """

    __tablename__ = "outbound_messages"

    __table_args__ = (
        UniqueConstraint(
            "msg_id",
            name="uq_outbound_message_msg_id",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)

    msg_id: str = Field(index=True, nullable=False)
    idpk: str = Field(index=True, nullable=False)

    message_type: str = Field(index=True, nullable=False)
    cycle_id: Optional[str] = Field(default=None, index=True)

    payload: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False),
    )

    # ACK/NACK/give/take pueden referenciar otro mensaje.
    target_msg_id: Optional[str] = Field(default=None, index=True)

    # Ruta de publicación cuando esté disponible.
    routing_key: Optional[str] = Field(default=None)

    exchange_name: str = Field(default="energy.x", nullable=False)

    # PENDING / PUBLISHED / FAILED
    status: str = Field(index=True, nullable=False)

    attempt_count: int = Field(default=0, nullable=False)

    available_at: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    expires_at: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )

    last_error: Optional[str] = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
    )

    created_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False, index=True)
    )

    published_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )

    # Indica que el mensaje fue creado por el backend y debe ser
    # publicado por el connector.
    dispatch_required: bool = Field(
        default=False,
        index=True,
        nullable=False,
    )

    # Para mensajes type=request identifica qué información se solicitó.
    request_ask: Optional[str] = Field(
        default=None,
        index=True,
    )

    # Correlación con la respuesta que resolvió el request.
    response_msg_id: Optional[str] = Field(
        default=None,
        index=True,
    )

    response_idpk: Optional[str] = Field(
        default=None,
        index=True,
    )

    resolved_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True),
            nullable=True,
        ),
    )


# =============================================================================
# Modelos v3 (E2-018): Mercado, Wallet, Swaps, Bids, Ofertas y Estrategias
# =============================================================================


class MarketOffer(SQLModel, table=True):
    """
    Oferta de venta de energía en el mercado (RF02, RF03).
    Aplica tanto a ofertas locales (publicadas por nuestra ciudad) como remotas
    (recibidas a través del feed market.{CODE}).
    """

    __tablename__ = "market_offers"

    __table_args__ = (
        UniqueConstraint("offer_id", name="uq_market_offers_offer_id"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)

    offer_id: str = Field(index=True, nullable=False)
    cycle_id: str = Field(index=True, nullable=False)
    city_id: str = Field(index=True, nullable=False)
    is_local: bool = Field(default=False, index=True, nullable=False)

    initial_quantity: int = Field(nullable=False)
    remaining_quantity: int = Field(nullable=False)
    price_per_energy: Decimal = Field(
        sa_column=Column(Numeric(20, 4), nullable=False)
    )

    # ACTIVE / WITHDRAWN / CLOSED
    status: str = Field(default="ACTIVE", index=True, nullable=False)

    published_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False, index=True)
    )
    last_updated_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    withdrawn_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )

    reservations: list["OfferReservation"] = Relationship(
        back_populates="offer",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )
    bids: list["MarketBid"] = Relationship(
        back_populates="offer",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )


class OfferReservation(SQLModel, table=True):
    """
    Reserva de energía de una oferta local para serialización estricta (AD4).
    Garantiza que dos bids concurrentes no comprometan el mismo KWH antes de
    la confirmación del swap en la wallet.
    """

    __tablename__ = "offer_reservations"

    __table_args__ = (
        UniqueConstraint("bid_idpk", name="uq_offer_reservations_bid_idpk"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)

    offer_id: str = Field(foreign_key="market_offers.offer_id", index=True, nullable=False)
    bid_idpk: str = Field(index=True, nullable=False)
    quantity: int = Field(nullable=False)

    # HOLD (comprometido en espera de swap) / COMMITTED (swap-settled) / RELEASED (rejected/expired/cancelled)
    status: str = Field(default="HOLD", index=True, nullable=False)

    created_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False, index=True)
    )
    released_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )

    offer: Optional[MarketOffer] = Relationship(back_populates="reservations")


class MarketBid(SQLModel, table=True):
    """
    Bid de compra/venta de energía (RF03, AD4).
    Registra tanto bids recibidos de compradores (somos seller) como enviados (somos buyer).
    """

    __tablename__ = "market_bids"

    __table_args__ = (
        UniqueConstraint("idpk", name="uq_market_bids_idpk"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)

    idpk: str = Field(index=True, nullable=False)
    msg_id: str = Field(index=True, nullable=False)
    cycle_id: str = Field(index=True, nullable=False)

    # INCOMING (recibido como vendedor) / OUTGOING (enviado como comprador)
    direction: str = Field(index=True, nullable=False)

    offer_id: str = Field(foreign_key="market_offers.offer_id", index=True, nullable=False)
    counterpart_city_id: str = Field(index=True, nullable=False)
    quantity: int = Field(nullable=False)

    # PENDING / ACCEPTED / REJECTED
    status: str = Field(default="PENDING", index=True, nullable=False)
    rejection_reason: Optional[str] = Field(default=None, index=True)
    available_on_rejection: Optional[int] = Field(default=None)

    swap_id: Optional[str] = Field(default=None, index=True)

    created_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False, index=True)
    )
    responded_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )

    offer: Optional[MarketOffer] = Relationship(back_populates="bids")


class SwapRecord(SQLModel, table=True):
    """
    Operación de swap gestionada a través de la wallet REST (AD5, RF03).
    Correlaciona idpk, swapId, tokens JWK ES256 y ciclo.
    """

    __tablename__ = "swap_records"

    __table_args__ = (
        UniqueConstraint("idpk", name="uq_swap_records_idpk"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)

    idpk: str = Field(index=True, nullable=False)
    swap_id: Optional[str] = Field(default=None, index=True)
    cycle_id: str = Field(index=True, nullable=False)

    # PROPOSER (nosotros propusimos el swap) / ACCEPTOR (nosotros lo aceptamos)
    role: str = Field(index=True, nullable=False)
    counterpart_city_id: str = Field(index=True, nullable=False)

    give_asset: str = Field(nullable=False)  # KWH o CR
    give_amount: Decimal = Field(
        sa_column=Column(Numeric(20, 4), nullable=False)
    )
    take_asset: str = Field(nullable=False)  # CR o KWH
    take_amount: Decimal = Field(
        sa_column=Column(Numeric(20, 4), nullable=False)
    )
    memo: Optional[str] = Field(default=None, index=True)

    # SUBMITTED (POST 202 enviado) / PROPOSED / ACCEPT_SUBMITTED / SETTLED / REJECTED / EXPIRED / CANCELLED
    status: str = Field(default="SUBMITTED", index=True, nullable=False)
    rejection_reason: Optional[str] = Field(default=None)

    idempotency_key: str = Field(index=True, nullable=False)
    accept_idempotency_key: Optional[str] = Field(default=None, index=True)
    swap_token: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    expires_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )

    created_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False, index=True)
    )
    settled_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )


class WalletBalanceSnapshot(SQLModel, table=True):
    """
    Snapshots y estado de saldo consultado desde la Wallet central (AD5, E2-022).
    La wallet es la autoridad económica de CR y KWH.
    """

    __tablename__ = "wallet_balance_snapshots"

    id: Optional[int] = Field(default=None, primary_key=True)

    cycle_id: Optional[str] = Field(default=None, index=True)
    # EVENT (derivado de swap-settled/trade-recorded/funding) / REST_SYNC (GET /balance)
    source: str = Field(index=True, nullable=False)

    kwh_available: Decimal = Field(
        default=Decimal("0.00"),
        sa_column=Column(Numeric(20, 4), nullable=False),
    )
    kwh_held: Decimal = Field(
        default=Decimal("0.00"),
        sa_column=Column(Numeric(20, 4), nullable=False),
    )
    cr_available: Decimal = Field(
        default=Decimal("0.00"),
        sa_column=Column(Numeric(20, 4), nullable=False),
    )
    cr_held: Decimal = Field(
        default=Decimal("0.00"),
        sa_column=Column(Numeric(20, 4), nullable=False),
    )
    credit_line: Decimal = Field(
        default=Decimal("0.00"),
        sa_column=Column(Numeric(20, 4), nullable=False),
    )

    recorded_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False, index=True)
    )


class StrategyConfig(SQLModel, table=True):
    """
    Configuración de estrategia de mercado editable sin redeploy (AD6, RF04).
    Cada edición genera una versión persistida que entra en vigencia en el siguiente ciclo.
    """

    __tablename__ = "strategy_configs"

    id: Optional[int] = Field(default=None, primary_key=True)

    version: int = Field(index=True, unique=True, nullable=False)
    # PENDING / ACTIVE / ARCHIVED
    status: str = Field(default="PENDING", index=True, nullable=False)

    offer_price_markup: Decimal = Field(
        default=Decimal("1.20"),
        sa_column=Column(Numeric(10, 4), nullable=False),
    )
    offer_quantity_fraction: Decimal = Field(
        default=Decimal("1.00"),
        sa_column=Column(Numeric(10, 4), nullable=False),
    )
    # FIFO, MAX_REMAINING, BATCH
    bid_acceptance_policy: str = Field(default="FIFO", nullable=False)
    # CHEAPEST_EFFECTIVE_PRICE, MIN_DISTANCE
    offer_selection_policy: str = Field(
        default="CHEAPEST_EFFECTIVE_PRICE", nullable=False
    )
    # Tiempo de espera antes de comprar en segundos (span para RF06)
    span_seconds: int = Field(default=60, nullable=False)

    effective_from_cycle_id: Optional[str] = Field(default=None, index=True)
    created_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False, index=True)
    )
    created_by: Optional[str] = Field(default=None)

