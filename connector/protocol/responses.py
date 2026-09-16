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