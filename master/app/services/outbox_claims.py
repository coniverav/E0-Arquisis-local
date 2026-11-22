"""Claim/lease concurrente del relay, sin efectos externos (E3-026).

El llamador debe: transacción claim_batch -> COMMIT -> I/O remoto ->
transacción finish_claim -> COMMIT. Nunca mantener locks DB durante HTTP/AMQP.
No activar junto con el dispatcher E2 sobre las mismas filas.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import and_, exists, or_
from sqlalchemy.orm import aliased
from sqlmodel import Session, select

from ..models import OutboundMessage

MARKET_TYPES = ("offer", "offer-updated", "offer-withdrawn")


def _execution_arn(value: str | None) -> str | None:
    if value is None:
        return None
    if not value.startswith("arn:aws:states:") or ":execution:" not in value:
        raise ValueError("INVALID_EXECUTION_ARN")
    return value


def _expected_execution_arn(row: OutboundMessage) -> str:
    prefix, separator, resource = (row.state_machine_arn or "").partition(
        ":stateMachine:"
    )
    machine_name = resource.split(":", 1)[0]
    if not separator or not machine_name or not row.execution_name:
        raise ValueError("INVALID_SAGA_DESTINATION")
    return f"{prefix}:execution:{machine_name}:{row.execution_name}"


def _utc(now: datetime | None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("NOW_MUST_HAVE_TIMEZONE")
    return value


def claim_batch(
    session: Session,
    *,
    worker_id: str,
    limit: int = 20,
    lease_seconds: int = 30,
    now: datetime | None = None,
    kinds: tuple[str, ...] = ("BROKER", "STEP_FUNCTIONS"),
) -> list[OutboundMessage]:
    """Reclama filas elegibles bloqueando con FOR UPDATE SKIP LOCKED.

    El caller hace COMMIT. La confirmación debe usar el claim_token de cada fila.
    """
    if not worker_id or not worker_id.strip():
        raise ValueError("WORKER_ID_REQUIRED")
    if limit < 1 or limit > 100 or lease_seconds < 1:
        raise ValueError("INVALID_CLAIM_LIMIT_OR_LEASE")
    if not kinds or set(kinds) - {"BROKER", "STEP_FUNCTIONS"}:
        raise ValueError("INVALID_DELIVERY_KIND")
    now = _utc(now)
    previous = aliased(OutboundMessage)
    prior_market_pending = exists(
        select(previous.id).where(
            previous.cycle_id == OutboundMessage.cycle_id,
            previous.delivery_kind == "BROKER",
            previous.message_type.in_(MARKET_TYPES),
            previous.id < OutboundMessage.id,
            previous.dispatch_required.is_(True),
            previous.status != "PUBLISHED",
        )
    )
    # Un lease vigente excluye la fila aunque el estado haya quedado en FAILED.
    lease_free = or_(
        OutboundMessage.lease_until.is_(None),
        OutboundMessage.lease_until <= now,
    )
    candidates = session.exec(
        select(OutboundMessage)
        .where(OutboundMessage.dispatch_required.is_(True))
        .where(OutboundMessage.delivery_kind.in_(kinds))
        .where(OutboundMessage.status.in_(("PENDING", "FAILED", "CLAIMED")))
        .where(lease_free)
        .where(or_(OutboundMessage.available_at.is_(None), OutboundMessage.available_at <= now))
        .where(or_(OutboundMessage.expires_at.is_(None), OutboundMessage.expires_at > now))
        .where(or_(
            OutboundMessage.message_type.not_in(MARKET_TYPES),
            ~prior_market_pending,
        ))
        .order_by(OutboundMessage.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).all()
    for row in candidates:
        row.status = "CLAIMED"
        row.lease_owner = worker_id
        row.claim_token = str(uuid4())
        row.lease_until = now + timedelta(seconds=lease_seconds)
        row.attempt_count += 1
        session.add(row)
    session.flush()
    return list(candidates)


def finish_claim(
    session: Session,
    *,
    row_id: int,
    claim_token: str,
    outcome: str,
    error: str | None = None,
    execution_arn: str | None = None,
    retry_at: datetime | None = None,
    now: datetime | None = None,
) -> bool:
    """Confirma sólo el claim vigente (fencing). True si se aplicó.

    Para Step Functions STARTED sólo debe invocarse tras StartExecution exitoso
    o ExecutionAlreadyExists corroborado para MISMA máquina/nombre/input.
    Para Broker PUBLISHED requiere publisher confirm (no sólo write()).
    """
    now = _utc(now)
    if outcome not in {"PUBLISHED", "STARTED", "FAILED"}:
        raise ValueError("INVALID_CLAIM_OUTCOME")
    if outcome == "FAILED" and not error:
        raise ValueError("FAILED_REQUIRES_ERROR")
    execution_arn = _execution_arn(execution_arn)
    if outcome == "STARTED" and execution_arn is None:
        raise ValueError("STARTED_REQUIRES_EXECUTION_ARN")
    if outcome != "STARTED" and execution_arn is not None:
        raise ValueError("EXECUTION_ARN_ONLY_FOR_STARTED")
    if retry_at is not None:
        retry_at = _utc(retry_at)
    row = session.exec(select(OutboundMessage).where(OutboundMessage.id == row_id).with_for_update()).first()
    if (row is None or row.status != "CLAIMED" or not claim_token
            or row.claim_token != claim_token or row.lease_until is None
            or row.lease_until <= now):
        return False
    if outcome == "PUBLISHED" and row.delivery_kind != "BROKER":
        raise ValueError("PUBLISHED_ONLY_FOR_BROKER")
    if outcome == "STARTED" and row.delivery_kind != "STEP_FUNCTIONS":
        raise ValueError("STARTED_ONLY_FOR_SAGA")
    if outcome == "STARTED" and execution_arn != _expected_execution_arn(row):
        raise ValueError("EXECUTION_ARN_DOES_NOT_MATCH_INTENT")
    if outcome == "FAILED":
        row.status = "FAILED"
        row.last_error = str(error)[:2000]
        row.available_at = retry_at or now
    else:
        row.status = outcome
        row.dispatch_required = False
        row.completed_at = now
        row.last_error = None
        if outcome == "PUBLISHED":
            row.published_at = now
        else:
            row.execution_arn = execution_arn
    row.lease_owner = None
    row.claim_token = None
    row.lease_until = None
    session.add(row)
    session.flush()
    return True
