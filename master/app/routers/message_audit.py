from datetime import datetime, timezone
from sqlalchemy import or_
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlmodel import Session, select

from ..database import get_session
from ..services.budget_carryover import lock_city_ledger
from ..models import (
    Cycle,
    InboundMessage,
    OutboundMessage,
    ProcessedIdpk,
    Negotiation,
)
from ..schemas import (
    AuditAnomalyListOut,
    AuditAnomalyOut,
    InboundMessageAuditIn,
    InboundMessageAuditListOut,
    InboundMessageAuditOut,
    OutboundMessageAuditIn,
    OutboundMessageResultIn,
)
from ..services.negotiation_report_dispatch import (
    mark_negotiation_report_sent,
    expire_report_dispatches,
)
from ..services.negotiation_state import (
    NEGOTIATION_PROPOSED,
    NEGOTIATION_PAID,
    transition_negotiation,
)
from ..services.message_audit import (
    INBOUND_DISCARDED,
    INBOUND_DUPLICATE,
    INBOUND_NACKED,
    OUTBOUND_FAILED,
    OUTBOUND_PENDING,
    mark_inbound_result,
    mark_outbound_failed,
    mark_outbound_published,
    record_inbound_message,
    record_outbound_message,
)

#En un duplicado interesa también conocer cuál fue el mensaje que realmente obtuvo el claim y aplicó la operación.
def _inbound_message_to_out(
    session: Session,
    message: InboundMessage,
) -> InboundMessageAuditOut:
    related_msg_id = message.msg_id

    if (
        message.status == INBOUND_DUPLICATE
        and message.idpk is not None
    ):
        processed = session.get(
            ProcessedIdpk,
            message.idpk,
        )

        if processed is not None:
            related_msg_id = processed.msg_id

    return InboundMessageAuditOut(
        id=message.id,
        msgId=message.msg_id,
        idpk=message.idpk,
        type=message.message_type,
        cycleId=message.cycle_id,
        status=message.status,
        reasonCode=message.reason_code,
        reason=message.reason,
        receivedAt=message.received_at,
        processedAt=message.processed_at,
        relatedMsgId=related_msg_id,
        payload=message.payload,
        rawPayload=message.raw_payload,
    )

def _inbound_message_to_anomaly_out(
    session: Session,
    message: InboundMessage,
) -> AuditAnomalyOut:
    internal = _inbound_message_to_out(
        session,
        message,
    )

    return AuditAnomalyOut(
        id=internal.id,
        msgId=internal.msgId,
        idpk=internal.idpk,
        type=internal.type,
        cycleId=internal.cycleId,
        status=internal.status,
        reasonCode=internal.reasonCode,
        reason=internal.reason,
        receivedAt=internal.receivedAt,
        processedAt=internal.processedAt,
        relatedMsgId=internal.relatedMsgId,
    )

router = APIRouter(
    prefix="/internal/audit",
    tags=["internal-audit"],
)

public_router = APIRouter(
    prefix="/audit",
    tags=["audit"],
)

@router.post("/outbound")
def create_outbound_audit(
    payload: OutboundMessageAuditIn,
    session: Session = Depends(get_session),
):
    """
    Registra durablemente la intención de publicar un mensaje.
    """

    existing = session.exec(
        select(OutboundMessage).where(
            OutboundMessage.msg_id == str(payload.msgId)
        )
    ).first()

    if existing is not None:
        return {
            "id": existing.id,
            "status": existing.status,
            "duplicate": True,
        }

    message = record_outbound_message(
        session,
        msg_id=str(payload.msgId),
        idpk=str(payload.idpk),
        message_type=payload.type,
        payload=payload.payload,
        cycle_id=payload.cycleId,
        target_msg_id=payload.targetMsgId,
        routing_key=payload.routingKey,
    )

    return {
        "id": message.id,
        "status": message.status,
        "duplicate": False,
    }

@router.get("/outbound/dispatch")
def list_outbound_dispatch(
    limit: int = Query(
        20,
        ge=1,
        le=100,
    ),
    session: Session = Depends(get_session),
):
    """
    Entrega al connector mensajes creados por el backend
    que todavía requieren publicación en RabbitMQ.
    """

    lock_city_ledger(session)
    now = datetime.now(timezone.utc)
    expire_report_dispatches(session, now)
    for proposal in session.exec(select(OutboundMessage).where(
        OutboundMessage.message_type == "negotiation-proposal",
        OutboundMessage.dispatch_required.is_(True),
    )).all():
        cycle = session.get(Cycle, proposal.cycle_id)
        if cycle is None or cycle.valid_until is None or cycle.closed_at is not None or now >= cycle.valid_until:
            proposal.dispatch_required = False
            proposal.last_error = "CYCLE_EXPIRED"
            negotiation = session.exec(select(Negotiation).where(Negotiation.idpk == proposal.idpk)).first()
            if negotiation is not None and negotiation.status in {"PENDING_PUBLICATION", "PROPOSED", "ACKNOWLEDGED"}:
                negotiation.deadline_at = cycle.valid_until if cycle is not None else now
                transition_negotiation(session, negotiation, "TIMEOUT", now=now)
    session.commit()
    open_report_cycles = select(Cycle.cycle_id).where(
        Cycle.status_idpk.is_not(None),
        Cycle.valid_until > now,
        Cycle.report_window_opens_at <= now,
        Cycle.scheduler_state != "CLOSED",
    )
    messages = session.exec(
        select(OutboundMessage)
        .where(
            OutboundMessage.dispatch_required.is_(True)
        )
        .where(
            OutboundMessage.status.in_(
                [
                    OUTBOUND_PENDING,
                    OUTBOUND_FAILED,
                ]
            )
        )
        .where(or_(OutboundMessage.available_at.is_(None), OutboundMessage.available_at <= now))
        .where(or_(OutboundMessage.expires_at.is_(None), OutboundMessage.expires_at > now))
        .where(or_(OutboundMessage.message_type != "negotiation-report",
                   OutboundMessage.cycle_id.in_(open_report_cycles)))
        .order_by(
            OutboundMessage.created_at.asc()
        )
        .limit(limit)
    ).all()

    return {
        "items": [
            {
                "msgId": message.msg_id,
                "idpk": message.idpk,
                "type": message.message_type,
                "routingKey": message.routing_key,
                "payload": message.payload,
                "expiresAt": message.expires_at,
            }
            for message in messages
        ]
    }

@router.post("/outbound/{msg_id}/result")
def update_outbound_audit(
    msg_id: str,
    payload: OutboundMessageResultIn,
    session: Session = Depends(get_session),
):
    """
    Registra el resultado del intento de publicación.
    """

    lock_city_ledger(session)
    message = session.exec(
        select(OutboundMessage).where(
            OutboundMessage.msg_id == msg_id
        )
    ).first()

    if message is None:
        raise HTTPException(
            status_code=404,
            detail="outbound message not found",
        )

    if message.message_type == "negotiation-report":
        # Coordinar con los errores de reporte: publicación y metadata se confirman
        # juntas. Los resultados tardíos no borran errores de negocio.
        session.exec(select(Cycle).where(Cycle.cycle_id == message.cycle_id).with_for_update()).first()
        session.refresh(message)
        if payload.status == "PUBLISHED":
            message.dispatch_required = False
            if message.last_error not in {"REPORT_TOO_EARLY", "CYCLE_EXPIRED", "CYCLE_UNKNOWN", "SUPERSEDED", "PRICE_ABOVE_CAP", "OVER_CAPACITY"}:
                message.last_error = None
            if message.status != "PUBLISHED":
                message.status = "PUBLISHED"
                message.attempt_count += 1
                message.published_at = datetime.now(timezone.utc)
            mark_negotiation_report_sent(session, msg_id=msg_id, sent_at=message.published_at)
        elif payload.error is None:
            raise HTTPException(status_code=422, detail="error is required for FAILED status")
        elif message.status != "PUBLISHED" and message.dispatch_required:
            message.status = "FAILED"
            message.last_error = payload.error
            message.attempt_count += 1
        session.commit()
        return {"id": message.id, "status": message.status, "attemptCount": message.attempt_count}

    if payload.status == "PUBLISHED":
        message.dispatch_required = False

        message = mark_outbound_published(
            session,
            message,
            commit=False,
        )

        if (message.message_type == "negotiation-proposal"):
            negotiation = session.exec(
                select(Negotiation).where(
                    Negotiation.latest_msg_id
                    == message.msg_id
                )
            ).first()

            if negotiation is not None and negotiation.status == "PENDING_PUBLICATION":
                transition_negotiation(
                    session,
                    negotiation,
                    NEGOTIATION_PROPOSED,
                    now=message.published_at,
                )
                session.flush()

        if (message.message_type == "transfer"
            and message.published_at is not None):

            negotiation = session.exec(
                select(Negotiation)
                .where(
                    Negotiation.latest_msg_id
                    == message.msg_id,
                    Negotiation.direction
                    == "take",
                )
                .with_for_update()
            ).first()

            if negotiation is not None:

                transition_negotiation(
                    session,
                    negotiation,
                    NEGOTIATION_PAID,
                    now=message.published_at,
                )

                session.flush()

    else:
        if payload.error is None:
            raise HTTPException(
                status_code=422,
                detail="error is required for FAILED status",
            )

        message = mark_outbound_failed(
            session,
            message,
            error=payload.error,
            commit=False,
        )

    session.commit()
    return {
        "id": message.id,
        "status": message.status,
        "attemptCount": message.attempt_count,
    }

#Persiste mensajes descartados o NACKeados por el connector antes de que lleguen al procesamiento normal del master.
@router.post(
    "/inbound",
    response_model=InboundMessageAuditOut,
)
def create_inbound_audit(
    payload: InboundMessageAuditIn,
    session: Session = Depends(get_session),
):
    message = record_inbound_message(
        session,
        msg_id=payload.msgId,
        idpk=payload.idpk,
        message_type=payload.type,
        payload=payload.payload,
        raw_payload=payload.rawPayload,
        cycle_id=payload.cycleId,
        sender=payload.sender,
    )

    mark_inbound_result(
        session,
        message,
        status=payload.status,
        reason_code=payload.reasonCode,
        reason=payload.reason,
    )

    return _inbound_message_to_out(
        session,
        message,
    )

#Consulta los mensajes relevantes para RF05, duplicados, descartados y NACKeados.
@router.get(
    "/inbound",
    response_model=InboundMessageAuditListOut,
)
def list_inbound_audit(
    status: Literal[
        "DUPLICATE",
        "DISCARDED",
        "NACKED",
    ] | None = None,
    message_type: str | None = Query(
        None,
        alias="type",
    ),
    reason_code: str | None = Query(
        None,
        alias="reasonCode",
    ),
    msg_id: str | None = Query(
        None,
        alias="msgId",
    ),
    idpk: str | None = None,
    limit: int = Query(
        100,
        ge=1,
        le=500,
    ),
    session: Session = Depends(get_session),
):
    target_statuses = [
        INBOUND_DUPLICATE,
        INBOUND_DISCARDED,
        INBOUND_NACKED,
    ]

    conditions = [
        InboundMessage.status.in_(
            target_statuses
        )
    ]

    if status is not None:
        conditions.append(
            InboundMessage.status == status
        )

    if message_type is not None:
        conditions.append(
            InboundMessage.message_type
            == message_type
        )

    if reason_code is not None:
        conditions.append(
            InboundMessage.reason_code
            == reason_code
        )

    if msg_id is not None:
        conditions.append(
            InboundMessage.msg_id == msg_id
        )

    if idpk is not None:
        conditions.append(
            InboundMessage.idpk == idpk
        )

    statement = select(InboundMessage)

    count_statement = select(
        func.count(InboundMessage.id)
    )

    for condition in conditions:
        statement = statement.where(
            condition
        )
        count_statement = (
            count_statement.where(
                condition
            )
        )

    total = session.exec(
        count_statement
    ).one()

    messages = session.exec(
        statement
        .order_by(
            InboundMessage.received_at.desc(),
            InboundMessage.id.desc(),
        )
        .limit(limit)
    ).all()

    return InboundMessageAuditListOut(
        total=total,
        items=[
            _inbound_message_to_out(
                session,
                message,
            )
            for message in messages
        ],
    )

@public_router.get(
    "/anomalies",
    response_model=AuditAnomalyListOut,
)
def list_audit_anomalies(
    status: Literal[
        "DUPLICATE",
        "DISCARDED",
        "NACKED",
    ] | None = None,
    message_type: str | None = Query(
        None,
        alias="type",
    ),
    reason_code: str | None = Query(
        None,
        alias="reasonCode",
    ),
    msg_id: str | None = Query(
        None,
        alias="msgId",
    ),
    idpk: str | None = None,
    limit: int = Query(
        100,
        ge=1,
        le=500,
    ),
    session: Session = Depends(get_session),
):
    target_statuses = [
        INBOUND_DUPLICATE,
        INBOUND_DISCARDED,
        INBOUND_NACKED,
    ]

    conditions = [
        InboundMessage.status.in_(
            target_statuses
        )
    ]

    if status is not None:
        conditions.append(
            InboundMessage.status == status
        )

    if message_type is not None:
        conditions.append(
            InboundMessage.message_type
            == message_type
        )

    if reason_code is not None:
        conditions.append(
            InboundMessage.reason_code
            == reason_code
        )

    if msg_id is not None:
        conditions.append(
            InboundMessage.msg_id == msg_id
        )

    if idpk is not None:
        conditions.append(
            InboundMessage.idpk == idpk
        )

    statement = select(InboundMessage)
    count_statement = select(
        func.count(InboundMessage.id)
    )

    for condition in conditions:
        statement = statement.where(
            condition
        )
        count_statement = (
            count_statement.where(
                condition
            )
        )

    total = session.exec(
        count_statement
    ).one()

    messages = session.exec(
        statement
        .order_by(
            InboundMessage.received_at.desc(),
            InboundMessage.id.desc(),
        )
        .limit(limit)
    ).all()

    return AuditAnomalyListOut(
        total=total,
        items=[
            _inbound_message_to_anomaly_out(
                session,
                message,
            )
            for message in messages
        ],
    )