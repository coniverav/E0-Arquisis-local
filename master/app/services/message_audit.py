from datetime import datetime, timezone
from typing import Any

from sqlmodel import Session

from app.models import InboundMessage, OutboundMessage

INBOUND_RECEIVED = "RECEIVED"
INBOUND_PROCESSED = "PROCESSED"
INBOUND_NOT_PROCESSED = "NOT_PROCESSED"
INBOUND_DUPLICATE = "DUPLICATE"
INBOUND_DISCARDED = "DISCARDED"
INBOUND_NACKED = "NACKED"
INBOUND_FAILED = "FAILED"

OUTBOUND_PENDING = "PENDING"
OUTBOUND_PUBLISHED = "PUBLISHED"
OUTBOUND_FAILED = "FAILED"


def record_inbound_message(
    session: Session,
    *,
    msg_id: str | None,
    idpk: str | None,
    message_type: str | None,
    payload: dict[str, Any] | None = None,
    raw_payload: str | None = None,
    cycle_id: str | None = None,
    sender: str | None = None,
) -> InboundMessage:
    """
    Persiste durablemente la recepción de un mensaje.

    Se realiza commit aquí para que la evidencia de recepción sobreviva
    aunque el procesamiento posterior falle.
    """
    message = InboundMessage(
        msg_id=msg_id,
        idpk=idpk,
        message_type=message_type,
        cycle_id=cycle_id,
        payload=payload,
        raw_payload=raw_payload,
        status=INBOUND_RECEIVED,
        sender=sender,
        received_at=datetime.now(timezone.utc),
    )

    session.add(message)
    session.commit()
    session.refresh(message)

    return message


def mark_inbound_result(
    session: Session,
    message: InboundMessage,
    *,
    status: str,
    reason_code: str | None = None,
    reason: str | None = None,
    commit: bool = True,
) -> InboundMessage:
    """Registra el resultado del procesamiento del mensaje recibido."""

    message.status = status
    message.reason_code = reason_code
    message.reason = reason
    message.processed_at = datetime.now(timezone.utc)

    session.add(message)

    if commit:
        session.commit()
        session.refresh(message)

    return message


def record_outbound_message(
    session: Session,
    *,
    msg_id: str,
    idpk: str,
    message_type: str,
    payload: dict[str, Any],
    cycle_id: str | None = None,
    target_msg_id: str | None = None,
    routing_key: str | None = None,
) -> OutboundMessage:
    """
    Persiste la intención de publicación antes de intentar enviarla al broker.
    """
    message = OutboundMessage(
        msg_id=msg_id,
        idpk=idpk,
        message_type=message_type,
        cycle_id=cycle_id,
        payload=payload,
        target_msg_id=target_msg_id,
        routing_key=routing_key,
        status=OUTBOUND_PENDING,
        attempt_count=0,
        created_at=datetime.now(timezone.utc),
    )

    session.add(message)
    session.commit()
    session.refresh(message)

    return message


def mark_outbound_published(
    session: Session,
    message: OutboundMessage,
) -> OutboundMessage:
    """
    Marca una publicación como exitosa.

    Si el mismo resultado PUBLISHED se informa nuevamente,
    no se contabiliza como un nuevo intento.
    """

    if message.status == OUTBOUND_PUBLISHED:
        return message

    message.status = OUTBOUND_PUBLISHED
    message.attempt_count += 1
    message.last_error = None
    message.published_at = datetime.now(timezone.utc)

    session.add(message)
    session.commit()
    session.refresh(message)

    return message


def mark_outbound_failed(
    session: Session,
    message: OutboundMessage,
    *,
    error: str,
) -> OutboundMessage:
    """
    Registra un intento fallido de publicación.

    Un mensaje ya publicado no vuelve a FAILED.
    Repetir exactamente el mismo resultado FAILED tampoco
    incrementa nuevamente attempt_count.
    """

    if message.status == OUTBOUND_PUBLISHED:
        return message

    if (
        message.status == OUTBOUND_FAILED
        and message.last_error == error
    ):
        return message

    message.status = OUTBOUND_FAILED
    message.attempt_count += 1
    message.last_error = error

    session.add(message)
    session.commit()
    session.refresh(message)

    return message