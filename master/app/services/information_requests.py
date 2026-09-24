from datetime import datetime, timezone
from uuid import uuid4

from sqlmodel import Session, select

from ..models import OutboundMessage
from .message_audit import (
    OUTBOUND_PENDING,
    OUTBOUND_PUBLISHED,
)


SUPPORTED_REQUESTS = {
    "status-statement",
    "distance-table",
}


def enqueue_information_request(
    session: Session,
    *,
    ask: str,
    city_id: str,
    routing_key: str,
    commit: bool = True,
) -> tuple[OutboundMessage, bool]:
    """
    Persiste un request para que posteriormente sea publicado
    por el connector.

    Si ya existe una solicitud pendiente de resolución para el
    mismo tipo de información, se reutiliza y no se crea otra.
    """

    if ask not in SUPPORTED_REQUESTS:
        raise ValueError(
            f"unsupported information request: {ask}"
        )

    if not city_id:
        raise ValueError("city_id is required")

    if not routing_key:
        raise ValueError("routing_key is required")

    existing = session.exec(
        select(OutboundMessage)
        .where(
            OutboundMessage.message_type == "request"
        )
        .where(
            OutboundMessage.request_ask == ask
        )
        .where(
            OutboundMessage.resolved_at.is_(None)
        )
        .order_by(
            OutboundMessage.created_at.asc()
        )
    ).first()

    if existing is not None:
        return existing, False

    now = datetime.now(timezone.utc)

    msg_id = str(uuid4())
    idpk = str(uuid4())

    payload = {
        "idpk": idpk,
        "msgId": msg_id,
        "type": "request",
        "timestamp": now.isoformat(),
        "cityId": city_id,
        "data": {
            "ask": ask,
        },
    }

    message = OutboundMessage(
        msg_id=msg_id,
        idpk=idpk,
        message_type="request",
        payload=payload,
        cycle_id=None,
        target_msg_id=None,
        routing_key=routing_key,
        status=OUTBOUND_PENDING,
        attempt_count=0,
        created_at=now,
        dispatch_required=True,
        request_ask=ask,
    )

    session.add(message)

    if commit:
        session.commit()
        session.refresh(message)
    else:
        session.flush()

    return message, True


def resolve_information_request(
    session: Session,
    *,
    response_type: str,
    response_msg_id: str,
    response_idpk: str,
) -> OutboundMessage | None:
    """
    Correlaciona la primera solicitud publicada y pendiente
    del tipo recibido con la respuesta de la central.
    """

    request = session.exec(
        select(OutboundMessage)
        .where(
            OutboundMessage.message_type == "request"
        )
        .where(
            OutboundMessage.request_ask == response_type
        )
        .where(
            OutboundMessage.status == OUTBOUND_PUBLISHED
        )
        .where(
            OutboundMessage.resolved_at.is_(None)
        )
        .order_by(
            OutboundMessage.created_at.asc()
        )
    ).first()

    if request is None:
        return None

    request.response_msg_id = response_msg_id
    request.response_idpk = response_idpk
    request.resolved_at = datetime.now(timezone.utc)

    session.add(request)
    session.flush()

    return request