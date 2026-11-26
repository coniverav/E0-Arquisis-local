"""API interna del relay AD8; los locks y el I/O remoto nunca se mezclan."""

from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session

from ..database import get_session
from ..models import OutboundMessage
from ..services.operational_rate_limits import reserve as reserve_rate_limit
from ..services.outbox_claims import claim_batch, finish_claim
from ..services.negotiation_report_dispatch import (
    mark_negotiation_report_sent, expire_report_dispatches, report_relay_enabled,
)
from ..services.budget_carryover import lock_city_ledger
from ..services.market_outbox import own_offer_relay_enabled
from ..services.seller_offer_events import expire_offer_dispatches


router = APIRouter(prefix="/internal/outbox-relay", tags=["internal"])


class RelayClaimIn(BaseModel):
    workerId: str = Field(min_length=1, max_length=160)
    deliveryKind: Literal["BROKER", "STEP_FUNCTIONS"]
    limit: int = Field(default=20, ge=1, le=100)
    leaseSeconds: int = Field(default=30, ge=1, le=300)


class RelayResultIn(BaseModel):
    claimToken: str = Field(min_length=1, max_length=128)
    outcome: Literal["PUBLISHED", "STARTED", "FAILED"]
    error: str | None = Field(default=None, max_length=2000)
    retryAfterSeconds: int | None = Field(default=None, ge=1, le=60)
    executionArn: str | None = Field(default=None, max_length=2048)


def _claimed(row: OutboundMessage) -> dict:
    base = {
        "id": row.id,
        "claimToken": row.claim_token,
        "deliveryKind": row.delivery_kind,
        "attemptCount": row.attempt_count,
        "msgId": row.msg_id,
        "idpk": row.idpk,
        "type": row.message_type,
        "cycleId": row.cycle_id,
        "expiresAt": row.expires_at,
        "leaseUntil": row.lease_until,
    }
    if row.delivery_kind == "BROKER":
        base.update({
            "exchange": row.exchange_name,
            "routingKey": row.routing_key,
            "payload": row.payload,
        })
    else:
        base.update({
            "stateMachineArn": row.state_machine_arn,
            "executionName": row.execution_name,
            "executionInput": row.execution_input,
        })
    return base


def _quota_category(row: OutboundMessage) -> str | None:
    if row.message_type in {"offer", "offer-updated", "offer-withdrawn"}:
        return "market:offer"
    if row.message_type == "request" and row.request_ask in {
        "status-statement", "distance-table",
    }:
        return "central:request"
    return None


@router.post("/claims")
def claim_outbox(
    payload: RelayClaimIn,
    session: Session = Depends(get_session),
):
    if payload.deliveryKind == "BROKER":
        lock_city_ledger(session)
        now = datetime.now(timezone.utc)
        expire_report_dispatches(session, now)
        expire_offer_dispatches(session, now=now)
    rows = claim_batch(
        session,
        worker_id=payload.workerId,
        limit=payload.limit,
        lease_seconds=payload.leaseSeconds,
        kinds=(payload.deliveryKind,),
        include_reports=report_relay_enabled(),
        include_offers=own_offer_relay_enabled(),
    )
    now = datetime.now(timezone.utc)
    deliverable = []
    for row in rows:
        category = _quota_category(row)
        retry_after = reserve_rate_limit(session, category, now=now) if category else 0
        if retry_after:
            # El mismo claim se cierra con fencing antes de responder. No se
            # entrega al relay ni se consume red mientras la cuota está llena.
            finish_claim(
                session,
                row_id=row.id,
                claim_token=row.claim_token,
                outcome="FAILED",
                error="OUTBOUND_RATE_LIMIT",
                retry_at=now + timedelta(seconds=retry_after),
                now=now,
            )
        else:
            deliverable.append(row)
    result = [_claimed(row) for row in deliverable]
    session.commit()
    return {"items": result}


@router.post("/{row_id}/result")
def finish_outbox(
    row_id: int,
    payload: RelayResultIn,
    session: Session = Depends(get_session),
):
    lock_city_ledger(session)
    now = datetime.now(timezone.utc)
    retry_at = (
        now + timedelta(seconds=payload.retryAfterSeconds)
        if payload.retryAfterSeconds is not None else None
    )
    try:
        changed = finish_claim(
            session,
            row_id=row_id,
            claim_token=payload.claimToken,
            outcome=payload.outcome,
            error=payload.error,
            retry_at=retry_at,
            execution_arn=payload.executionArn,
            now=now,
        )
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not changed:
        session.rollback()
        raise HTTPException(status_code=409, detail="STALE_OUTBOX_CLAIM")
    row = session.get(OutboundMessage, row_id)
    if (
        payload.outcome == "PUBLISHED"
        and row is not None
        and row.message_type == "negotiation-report"
    ):
        # Conserva el efecto observable que tenía el dispatcher E2, en la
        # misma transacción que confirma el claim con fencing.
        mark_negotiation_report_sent(
            session,
            msg_id=row.msg_id,
            sent_at=row.published_at,
        )
    session.commit()
    return {"status": payload.outcome, "updated": True}
