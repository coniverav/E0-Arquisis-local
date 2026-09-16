# Define el esquema de la base de datos, es decir, las tablas events y demands

from datetime import datetime
from typing import Any, Optional
from decimal import Decimal

from sqlalchemy import Column, DateTime, Text, Numeric, UniqueConstraint
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
        unique=True,
        index=True,
        nullable=False,
    )

    msg_id: str = Field(unique=True, nullable=False)
    idpk: str = Field(unique=True, nullable=False)

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