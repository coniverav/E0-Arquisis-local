from datetime import datetime, timezone
from uuid import uuid4

from .models import ProtocolEnvelope


NO_ACK_TYPES = {"ack", "nack", "error"}

#Indica si corresponde responder el mensaje con un ACK.
def should_send_ack(message: ProtocolEnvelope) -> bool:
    return message.type not in NO_ACK_TYPES

#Construye un ACK del protocolo v2 para un mensaje recibido.
def build_ack(
    message: ProtocolEnvelope,
    city_id: str,
    timestamp: datetime | None = None,
) -> dict:

    created_at = timestamp or datetime.now(timezone.utc)

    return {
        "idpk": str(uuid4()),
        "msgId": str(uuid4()),
        "type": "ack",
        "timestamp": created_at.isoformat().replace("+00:00", "Z"),
        "cityId": city_id,
        "data": {
            "target": str(message.msg_id),
        },
    }

#Construye un NACK del protocolo v2 para un mensaje rechazado.
def build_nack(
    target_msg_id: str,
    reason: str,
    code: int,
    message: str,
    city_id: str,
    cycle_id: str | None = None,
    timestamp: datetime | None = None,
) -> dict:

    created_at = timestamp or datetime.now(timezone.utc)

    data = {
        "target": target_msg_id,
        "message": message,
    }

    if cycle_id is not None:
        data["cycleId"] = cycle_id

    return {
        "idpk": str(uuid4()),
        "msgId": str(uuid4()),
        "type": "nack",
        "timestamp": created_at.isoformat().replace("+00:00", "Z"),
        "cityId": city_id,
        "reason": reason,
        "code": code,
        "data": data,
    }