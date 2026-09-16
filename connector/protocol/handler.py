from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from .models import ProtocolEnvelope
from .parser import EnvelopeParseError, MissingMsgIdError, parse_envelope
from .responses import build_ack, build_nack, should_send_ack
from .validation import validate_message

#Resultado de clasificar un mensaje recibido.
@dataclass(frozen=True)
class ProtocolHandlingResult:
    action: Literal["ack", "nack", "ignore", "discard"]
    message: ProtocolEnvelope | None = None
    response: dict | None = None

#Clasifica un payload y construye la respuesta de protocolo cuando corresponda.
#Esta función no publica en RabbitMQ, solamente decide la acción y construye ACK/NACK para que la capa de transporte los envíe cuando corresponda.
def plan_protocol_response(
    payload: dict,
    city_id: str,
    timestamp: datetime | None = None,
) -> ProtocolHandlingResult:
    try:
        message = parse_envelope(payload)

    except MissingMsgIdError:
        return ProtocolHandlingResult(action="discard")

    except EnvelopeParseError as exc:
        #Existe msgId, por lo que podemos responder con MALFORMED_MESSAGE.
        response = build_nack(
            target_msg_id=str(payload["msgId"]),
            reason="MALFORMED_MESSAGE",
            code=422,
            message=str(exc),
            city_id=city_id,
            cycle_id=payload.get("cycleId"),
            timestamp=timestamp,
        )

        return ProtocolHandlingResult(
            action="nack",
            response=response,
        )

    validation = validate_message(message)

    if not validation.valid:
        if validation.reason is None or validation.code is None:
            raise ValueError("Una validación inválida debe incluir reason y code")

        response = build_nack(
            target_msg_id=str(message.msg_id),
            reason=validation.reason,
            code=validation.code,
            message=validation.message or "Mensaje inválido",
            city_id=city_id,
            cycle_id=message.cycle_id,
            timestamp=timestamp,
        )

        return ProtocolHandlingResult(
            action="nack",
            message=message,
            response=response,
        )

    if not should_send_ack(message):
        return ProtocolHandlingResult(
            action="ignore",
            message=message,
        )

    response = build_ack(
        message=message,
        city_id=city_id,
        timestamp=timestamp,
    )

    return ProtocolHandlingResult(
        action="ack",
        message=message,
        response=response,
    )