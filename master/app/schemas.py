# Define esquemas Pydantic, es decir, qué forma deben tener los JSON que entran y salen de la API

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class DemandPayload(BaseModel):
    city: str
    demand: float
    unit: str


class PackageBodyPayload(BaseModel):
    demands: list[DemandPayload]
    validUntil: datetime
    metaContent: str | None = None
    constraints: dict[str, Any] = Field(default_factory=dict)

    @field_validator("validUntil")
    @classmethod
    def valid_until_must_have_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("validUntil debe incluir zona horaria (ISO8601 UTC)")
        return value


class EventPayload(BaseModel):
    idpk: UUID
    type: Literal["demand-set"]
    packageBody: PackageBodyPayload


class EventOut(BaseModel):
    id: int
    idpk: UUID
    type: str
    packageBody: PackageBodyPayload
    receivedAt: datetime


class HistoryOut(BaseModel):
    page: int
    limit: int
    total: int
    items: list[EventOut]
