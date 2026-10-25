"""Validación mínima del feed antes de entregarlo al inbox durable.

La validación comercial y la aplicación de ofertas pertenecen a otras tareas
El feed no produce respuestas ACK/NACK del protocolo.
"""

import math
import re

from .intake import DiscardMessage
from .parser import EnvelopeParseError, parse_envelope


MARKET_FEED_TYPES = frozenset(
    {"offer", "offer-updated", "offer-withdrawn", "market-closed"}
)
CITY_CODE = re.compile(r"^[A-Z0-9]{3}$")


def validate_market_feed(payload: dict, *, amqp_user_id: str | None) -> None:
    """Comprueba envelope, tipo e identidad sin usar el validador E1."""
    try:
        envelope = parse_envelope(payload)
    except EnvelopeParseError as exc:
        raise DiscardMessage(str(exc), payload=payload) from exc

    if envelope.type not in MARKET_FEED_TYPES:
        raise DiscardMessage("Tipo ajeno al feed de mercado", payload=payload)
    if envelope.idpk == envelope.msg_id:
        raise DiscardMessage("idpk y msgId deben ser distintos", payload=payload)
    if envelope.timestamp.tzinfo is None:
        raise DiscardMessage("timestamp requiere zona horaria", payload=payload)
    if not isinstance(envelope.cycle_id, str) or not envelope.cycle_id:
        raise DiscardMessage("cycleId es obligatorio", payload=payload)
    if not isinstance(envelope.data, dict):
        raise DiscardMessage("data debe ser un objeto", payload=payload)

    if envelope.type == "market-closed":
        if (envelope.sender != "central" or "cityId" in payload
                or amqp_user_id is not None or envelope.data != {}
                or set(payload) - {"idpk", "msgId", "type", "timestamp", "sender", "cycleId", "data"}):
            raise DiscardMessage("market-closed requiere sender central", payload=payload)
        return

    if (
        not isinstance(envelope.city_id, str)
        or not CITY_CODE.fullmatch(envelope.city_id)
        or envelope.sender is not None
    ):
        raise DiscardMessage("Oferta requiere cityId válido", payload=payload)
    if amqp_user_id != f"city.{envelope.city_id}":
        raise DiscardMessage("AMQP user_id no corresponde al cityId", payload=payload)

    data = envelope.data
    if not isinstance(data.get("offerId"), str) or not data["offerId"]:
        raise DiscardMessage("data.offerId debe ser un string no vacío", payload=payload)
    if envelope.type in {"offer", "offer-updated"}:
        field = "quantity" if envelope.type == "offer" else "remaining"
        quantity = data.get(field)
        if not isinstance(quantity, int) or isinstance(quantity, bool):
            raise DiscardMessage(f"data.{field} debe ser entero", payload=payload)
        price = data.get("pricePerEnergy")
        if (not isinstance(price, (int, float)) or isinstance(price, bool)
                or (isinstance(price, float) and not math.isfinite(price))):
            raise DiscardMessage("data.pricePerEnergy debe ser numérico finito", payload=payload)
