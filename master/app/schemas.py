# Define esquemas Pydantic, es decir, qué forma deben tener los JSON que entran y salen de la API

from datetime import datetime
from typing import Any, Literal
from uuid import UUID
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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


class LedgerEntryOut(BaseModel):
    sequence: int
    operationType: str

    budgetDelta: Decimal
    energyDelta: Decimal

    budgetAfter: Decimal
    energyAfter: Decimal

    appliedAt: datetime
    details: dict[str, Any]

class NegotiationOut(BaseModel):
    id: int
    idpk: str
    cycleId: str

    direction: str
    requestedQuantity: Decimal
    offeredPrice: Decimal
    status: str

    confirmedEnergy: Decimal | None = None
    confirmedPrice: Decimal | None = None
    paymentQuantity: Decimal | None = None

    deadlineAt: datetime | None = None

    createdAt: datetime
    updatedAt: datetime

class NegotiationsOut(BaseModel):
    total: int
    items: list[NegotiationOut]

class NegotiationCreate(BaseModel):
    cycleId: str
    idpk: UUID

    direction: Literal["give", "take"]
    requestedQuantity: Decimal = Field(gt=0)
    offeredPrice: Decimal = Field(ge=0)

class NegotiationReportOut(BaseModel):
    status: Literal["PENDING", "PUBLISHED", "DEFERRED", "EXPIRED", "REJECTED", "SUPERSEDED"] = "PENDING"
    reason: str | None = None
    msgId: str
    idpk: str
    budgetBalance: Decimal
    energyBalance: Decimal
    createdAt: datetime
    sentAt: datetime | None = None

class CycleSummaryOut(BaseModel):
    negotiationOpen: bool = False
    validUntil: datetime | None = None
    cycleId: str

    budgetBalance: Decimal
    energyBalance: Decimal

    lastOperationType: str | None
    lastOperationAt: datetime | None

    reported: bool

class CyclesOut(BaseModel):
    total: int
    items: list[CycleSummaryOut]

class CycleDetailOut(BaseModel):
    negotiationOpen: bool = False
    validUntil: datetime | None = None

    cycleId: str

    statusStatement: dict[str, Any]

    fundsReceived: list[LedgerEntryOut]
    demandStatements: list[LedgerEntryOut]

    negotiations: list[NegotiationOut]

    negotiationReports: list[NegotiationReportOut] = Field(default_factory=list)
    negotiationReport: NegotiationReportOut | None

    finalBudgetBalance: Decimal
    finalEnergyBalance: Decimal

    lastOperation: LedgerEntryOut | None

    reconstructedBudgetBalance: Decimal
    reconstructedEnergyBalance: Decimal
    snapshotConsistent: bool

    ledger: list[LedgerEntryOut]

class DistanceTableOut(BaseModel):
    id: int
    msgId: str
    idpk: str
    timestamp: datetime
    receivedAt: datetime
    distances: dict[str, Any]

class ConnectivityItemOut(BaseModel):
    destination: str
    distance: float
    transportCost: float
    enabled: bool


class ConnectivityOut(BaseModel):
    timestamp: datetime
    total: int
    items: list[ConnectivityItemOut]


class ProtocolMessageIn(BaseModel):
    """
    Envelope base de los mensajes del protocolo E1.
    """

    # Conservar metadata opcional del broker, incluido penalty, sin suponer su estructura.
    model_config = ConfigDict(extra="allow")

    idpk: UUID
    msgId: UUID
    type: str
    timestamp: datetime

    # Los mensajes asociados a ciclos lo incluyen.
    cycleId: str | None = None

    # Mensajes enviados por la central.
    sender: str | None = None

    # Mensajes enviados por nuestra ciudad.
    cityId: str | None = None

    # En E1 el contenido viaja en data,
    # reemplazando packageBody de E0.
    data: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def report_requires_cycle(self):
        if self.type == "negotiation-report" and not self.cycleId:
            raise ValueError("MALFORMED_MESSAGE: negotiation-report requiere cycleId")
        return self

    @field_validator("timestamp")
    @classmethod
    def timestamp_must_have_timezone(
        cls,
        value: datetime,
    ) -> datetime:

        if value.tzinfo is None:
            raise ValueError(
                "timestamp debe incluir zona horaria"
            )

        return value

#Códigos de error
ERROR_CODES = {
    "REPORT_TOO_EARLY": (422, 425),
    "CYCLE_UNKNOWN": 404,
    "CYCLE_EXPIRED": 410,
    "PRICE_ABOVE_CAP": 422,
    "OVER_CAPACITY": 409,
}


class ProtocolErrorDataPayload(BaseModel):
    opensAt: datetime | None = None

    @field_validator("opensAt")
    @classmethod
    def opens_at_has_timezone(cls, value):
        if value is not None and value.tzinfo is None:
            raise ValueError("opensAt debe incluir zona horaria")
        return value

    target: UUID
    message: str
    cap: float | None = None
    spare: float | None = None


class ProtocolErrorPayload(BaseModel):
    idpk: UUID
    msgId: UUID
    type: Literal["error"]
    timestamp: datetime
    sender: Literal["central"]
    cycleId: str

    reason: Literal[
        "REPORT_TOO_EARLY",
        "CYCLE_UNKNOWN",
        "CYCLE_EXPIRED",
        "PRICE_ABOVE_CAP",
        "OVER_CAPACITY",
    ]

    code: int
    data: ProtocolErrorDataPayload

    @field_validator("timestamp")
    @classmethod
    def timestamp_must_have_timezone(
        cls,
        value: datetime,
    ) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamp debe incluir zona horaria")

        return value

    @model_validator(mode="after")
    def validate_error(self):
        if self.reason == "REPORT_TOO_EARLY" and self.data.opensAt is None:
            raise ValueError("REPORT_TOO_EARLY requiere data.opensAt")
        expected_code = ERROR_CODES[self.reason]

        codes = expected_code if isinstance(expected_code, tuple) else (expected_code,)
        if self.code not in codes:
            raise ValueError(
                f"{self.reason} requiere code {expected_code}"
            )

        if (
            self.reason == "PRICE_ABOVE_CAP"
            and self.data.cap is None
        ):
            raise ValueError(
                "PRICE_ABOVE_CAP requiere data.cap"
            )

        if (
            self.reason == "OVER_CAPACITY"
            and self.data.spare is None
        ):
            raise ValueError(
                "OVER_CAPACITY requiere data.spare"
            )

        return self


class ProtocolErrorOut(BaseModel):
    id: int
    idpk: UUID
    msgId: UUID
    cycleId: str
    reason: str
    code: int
    target: UUID
    message: str
    cap: float | None = None
    spare: float | None = None
    timestamp: datetime
    receivedAt: datetime

class OutboundMessageAuditIn(BaseModel):
    msgId: UUID
    idpk: UUID
    type: str

    cycleId: str | None = None
    targetMsgId: str | None = None
    routingKey: str | None = None

    payload: dict[str, Any] = Field(default_factory=dict)

class OutboundMessageResultIn(BaseModel):
    status: Literal["PUBLISHED", "FAILED"]
    error: str | None = None

#Evidencia de un mensaje rechazado por el connector antes de llegar al procesamiento normal del master.
class InboundMessageAuditIn(BaseModel):
    status: Literal["DISCARDED", "NACKED"]

    msgId: str | None = None
    idpk: str | None = None
    type: str | None = None
    cycleId: str | None = None
    sender: str | None = None

    payload: dict[str, Any] | None = None
    rawPayload: str | None = None

    reasonCode: str | None = None
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_nacked_reason_code(self):
        if self.status == "NACKED" and self.reasonCode is None:
            raise ValueError(
                "NACKED requiere reasonCode"
            )

        return self

class InboundMessageAuditOut(BaseModel):
    id: int

    msgId: str | None
    idpk: str | None
    type: str | None
    cycleId: str | None

    status: str
    reasonCode: str | None
    reason: str | None

    receivedAt: datetime
    processedAt: datetime | None

    #Para DUPLICATE apunta al mensaje que obtuvo originalmente el claim del idpk. Para NACKED/DISCARDED identifica, cuando
    #existe, el mensaje recibido que produjo el registro.
    relatedMsgId: str | None

    payload: dict[str, Any] | None
    rawPayload: str | None

class InboundMessageAuditListOut(BaseModel):
    total: int
    items: list[InboundMessageAuditOut]

#Vista pública reducida de anomalías relevantes para RF05.
class AuditAnomalyOut(BaseModel):
    id: int

    msgId: str | None
    idpk: str | None
    type: str | None
    cycleId: str | None

    status: Literal[
        "DUPLICATE",
        "DISCARDED",
        "NACKED",
    ]

    reasonCode: str | None
    reason: str | None

    receivedAt: datetime
    processedAt: datetime | None
    relatedMsgId: str | None


class AuditAnomalyListOut(BaseModel):
    total: int
    items: list[AuditAnomalyOut]
