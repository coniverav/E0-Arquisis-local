from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from .models import ProtocolEnvelope
from .parser import EnvelopeParseError, MissingMsgIdError, parse_envelope
from .responses import build_ack, build_nack, should_send_ack
from .validation import CITY_CODE, validate_message

#Resultado de clasificar un mensaje recibido.
@dataclass(frozen=True)
class ProtocolHandlingResult:
    action: Literal["ack", "nack", "ignore", "discard"]
    message: ProtocolEnvelope | None = None
    response: dict | None = None
    discard_reason: str | None = None

#Clasifica un payload y construye la respuesta de protocolo cuando corresponda.
#Esta función no publica en RabbitMQ, solamente decide la acción y construye ACK/NACK para que la capa de transporte los envíe cuando corresponda.
def plan_protocol_response(
    payload: dict,
    city_id: str,
    timestamp: datetime | None = None,
    amqp_user_id: str | None = None,
) -> ProtocolHandlingResult:
    try:
        message = parse_envelope(payload)

    except MissingMsgIdError as exc:
        return ProtocolHandlingResult(action="discard", discard_reason=str(exc))

    except EnvelopeParseError as exc:
        if payload.get("type") in ("ack", "nack", "error"):
            return ProtocolHandlingResult(action="discard", discard_reason=str(exc))
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

    #La identidad se contrasta antes del contenido: un bid falsificado nunca llega al backend, incluso si su data también está malformada.
    if (message.idpk != message.msg_id and isinstance(message.city_id, str)
            and CITY_CODE.fullmatch(message.city_id) and message.sender is None):
        if amqp_user_id != f"city.{message.city_id}":
            if message.type in {"ack", "nack", "error"}:
                return ProtocolHandlingResult(action="discard", message=message,
                                              discard_reason="Identidad inválida en respuesta de protocolo")
            return ProtocolHandlingResult(action="nack", message=message, response=build_nack(
                target_msg_id=str(message.msg_id), reason="IDENTITY_MISMATCH", code=403,
                message="AMQP user_id no corresponde al cityId", city_id=city_id,
                cycle_id=message.cycle_id, timestamp=timestamp,
            ))

    if message.sender == "central" and message.city_id is None and amqp_user_id is not None:
        if message.type in {"ack", "nack", "error"}:
            return ProtocolHandlingResult(action="discard", message=message,
                                          discard_reason="Respuesta que declara sender central con AMQP user_id")
        return ProtocolHandlingResult(action="nack", message=message, response=build_nack(
            target_msg_id=str(message.msg_id), reason="MALFORMED_MESSAGE", code=422,
            message="Un mensaje de ciudad no puede declarar sender central", city_id=city_id,
            cycle_id=message.cycle_id, timestamp=timestamp,
        ))

    validation = validate_message(message)

    if not validation.valid:
        if message.type in {"ack", "nack", "error"}:
            return ProtocolHandlingResult(action="discard", message=message,
                                          discard_reason=validation.message)
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
