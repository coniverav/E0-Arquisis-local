from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from ..database import get_session
from ..models import OutboundMessage
from ..schemas import (
    OutboundMessageAuditIn,
    OutboundMessageResultIn,
)
from ..services.message_audit import (
    mark_outbound_failed,
    mark_outbound_published,
    record_outbound_message,
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
