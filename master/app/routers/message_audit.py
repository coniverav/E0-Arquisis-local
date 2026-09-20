from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlmodel import Session, select

from ..database import get_session
from ..models import (
    InboundMessage,
    OutboundMessage,
    ProcessedIdpk,
)
from ..schemas import (
    InboundMessageAuditIn,
    InboundMessageAuditListOut,
    InboundMessageAuditOut,
    OutboundMessageAuditIn,
    OutboundMessageResultIn,
)
from ..services.message_audit import (
    INBOUND_DISCARDED,
    INBOUND_DUPLICATE,
    INBOUND_NACKED,
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

router = APIRouter(
    prefix="/internal/audit",
    tags=["internal-audit"],
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


@router.post("/outbound/{msg_id}/result")
def update_outbound_audit(
    msg_id: str,
    payload: OutboundMessageResultIn,
    session: Session = Depends(get_session),
):
    """
    Registra el resultado del intento de publicación.
    """

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

    if payload.status == "PUBLISHED":
        message = mark_outbound_published(
            session,
            message,
        )

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
        )

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