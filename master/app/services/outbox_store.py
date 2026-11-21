"""Persistencia transaccional E3-025 para Broker y StartExecution.

IMPORTANTE: nunca hacemos commit; el productor inserta negocio + outbox en
una transacción de SQLAlchemy/SQLModel y el dueño de la sesión hace commit.
No invocar servicios externos en este módulo.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any, Mapping
from uuid import uuid4

from sqlmodel import Session

from ..models import OutboundMessage
from .outbox_contract import (
    canonical_execution_input,
    validate_broker_envelope,
    validate_execution_identity,
)


def enqueue_broker(
    session: Session,
    *,
    envelope: Mapping[str, Any],
    exchange_name: str,
    routing_key: str,
    available_at: datetime | None = None,
    expires_at: datetime | None = None,
    swap_id: str | None = None,
    now: datetime | None = None,
) -> OutboundMessage:
    payload = validate_broker_envelope(envelope)
    if not exchange_name or not routing_key:
        raise ValueError("BROKER_DESTINATION_REQUIRED")
    record = OutboundMessage(
        msg_id=payload["msgId"],
        idpk=payload["idpk"],
        message_type=payload["type"],
        cycle_id=payload["cycleId"],
        swap_id=swap_id,
        payload=payload,
        delivery_kind="BROKER",
        exchange_name=exchange_name,
        routing_key=routing_key,
        status="PENDING",
        attempt_count=0,
        dispatch_required=True,
        created_at=now or datetime.now(timezone.utc),
        available_at=available_at,
        expires_at=expires_at,
    )
    session.add(record)
    session.flush()
    return record


def enqueue_saga_start(
    session: Session,
    *,
    idpk: str,
    role: str,
    state_machine_arn: str,
    execution_input: Mapping[str, Any] | str,
    cycle_id: str,
    swap_id: str | None = None,
    available_at: datetime | None = None,
    expires_at: datetime | None = None,
    now: datetime | None = None,
) -> OutboundMessage:
    execution_name = validate_execution_identity(idpk)
    if role not in {"SELLER", "BUYER"}:
        raise ValueError("INVALID_SAGA_ROLE")
    if not state_machine_arn or not state_machine_arn.startswith("arn:aws:states:"):
        raise ValueError("STATE_MACHINE_ARN_REQUIRED")
    if not cycle_id:
        raise ValueError("CYCLE_ID_REQUIRED")
    serialized = canonical_execution_input(execution_input)
    decoded = json.loads(serialized)
    if (decoded.get("idpk") != idpk or decoded.get("cycleId") != cycle_id
            or decoded.get("role") != role):
        raise ValueError("SAGA_INPUT_MUST_MATCH_INTENT")
    record = OutboundMessage(
        msg_id=str(uuid4()),          # ID de esta intención interna, persistido una sola vez
        idpk=idpk,                  # el idpk recibido inicia la ejecución
        message_type="SAGA_START",
        cycle_id=cycle_id,
        swap_id=swap_id,
        payload={},                # campo legado JSONB NOT NULL (no es un envelope AMQP)
        delivery_kind="STEP_FUNCTIONS",
        state_machine_arn=state_machine_arn,
        execution_name=execution_name,
        execution_input=serialized, # texto JSON exacto a enviar por StartExecution
        status="PENDING",
        attempt_count=0,
        dispatch_required=True,
        created_at=now or datetime.now(timezone.utc),
        available_at=available_at,
        expires_at=expires_at,
    )
    session.add(record)
    session.flush()
    return record
