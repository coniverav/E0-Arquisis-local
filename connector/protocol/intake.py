import json
import math
from uuid import UUID

#Mensaje que debe descartarse sin generar una respuesta de protocolo.
class DiscardMessage(ValueError):
    def __init__(
        self,
        message: str,
        payload: dict | None = None,
    ):
        super().__init__(message)
        self.payload = payload

def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Número JSON fuera del rango finito")
    return number


def _invalid_constant(value: str):
    raise ValueError(f"Constante JSON inválida: {value}")


#Decodifica el body recibido desde RabbitMQ.
#Solo clasifica los casos que deben descartarse sin respuesta. Mensajes no parseables y mensajes sin msgId.
def decode_incoming_payload(body: bytes) -> dict:
    try:
        payload = json.loads(body.decode("utf-8"), parse_float=_finite_float, parse_constant=_invalid_constant)
    except (UnicodeDecodeError, ValueError) as exc:
        raise DiscardMessage("El mensaje no es JSON válido") from exc

    #Un envelope del protocolo debe ser un objeto JSON.
    if not isinstance(payload, dict):
        raise DiscardMessage("El mensaje JSON no es un objeto")

    #Sin msgId no existe un mensaje válido al cual responder.
    if "msgId" not in payload:
        raise DiscardMessage("El mensaje no contiene msgId", payload=payload,)

    try:
        UUID(str(payload["msgId"]))
    except (ValueError, TypeError, AttributeError) as exc:
        raise DiscardMessage("El mensaje no contiene un msgId UUID válido", payload=payload) from exc

    return payload