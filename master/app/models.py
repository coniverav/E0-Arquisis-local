# Define el esquema de la base de datos, es decir, las tablas events y demands

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import Column, DateTime, Text
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
