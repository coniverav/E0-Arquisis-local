"""Rutas y comprobaciones de forma para publicaciones salientes de ciudad."""

from datetime import datetime
import math
import re
from typing import Callable
from uuid import UUID


MARKET_TYPES = frozenset({"offer", "offer-updated", "offer-withdrawn"})
CITY_TYPES = frozenset({"bid", "bid-accepted", "bid-rejected"})
CENTRAL_TYPES = frozenset({"request", "negotiation-report"})
RESPONSE_TYPES = frozenset({"ack", "nack"})
V3_TYPES = MARKET_TYPES | CITY_TYPES | CENTRAL_TYPES | RESPONSE_TYPES
LEGACY_DIRECTED_TYPES = frozenset({"negotiation-proposal", "give", "take", "transfer"})
CITY_ROUTING_KEY = re.compile(r"^city\.[A-Z0-9]{3}$")
NACK_CODES = {
    "MALFORMED_MESSAGE": 422,
    "UNKNOWN_TYPE": 400,
    "IDPK_EQUALS_MSGID": 422,
    "IDENTITY_MISMATCH": 403,
}
BID_REJECTION_CODES = {
    "QUANTITY_UNAVAILABLE": 409,
    "OFFER_WITHDRAWN": 410,
    "BID_TOO_SMALL": 422,
    "MARKET_CLOSED": 410,
    "SELF_TRADE": 422,
}


def _uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        UUID(value)
    except ValueError:
        return False
    return True


def _instant(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _number(value: object) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _required(data: dict, fields: dict[str, Callable[[object], bool]], message_type: str) -> None:
    for field, check in fields.items():
        if not check(data.get(field)):
            raise ValueError(f"{message_type}.data.{field} no cumple el contrato v3")


def _validate_v3(payload: dict, message_type: str, city_id: str) -> None:
    if payload.get("cityId") != city_id:
        raise ValueError("cityId saliente no corresponde a esta ciudad")
    if not _uuid(payload.get("idpk")) or not _uuid(payload.get("msgId")):
        raise ValueError("idpk y msgId deben ser UUID válidos")
    if payload["idpk"] == payload["msgId"]:
        raise ValueError("idpk y msgId deben ser distintos")
    if not _instant(payload.get("timestamp")):
        raise ValueError("timestamp debe ser ISO 8601 con zona horaria")
    if message_type in MARKET_TYPES | CITY_TYPES | {"negotiation-report"}:
        if not _text(payload.get("cycleId")):
            raise ValueError("cycleId es obligatorio")
    elif "cycleId" in payload and not isinstance(payload["cycleId"], str):
        raise ValueError("cycleId debe ser string")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("data debe ser un objeto")

    fields = {
        "offer": {"offerId": _text, "quantity": _integer, "pricePerEnergy": _number},
        "offer-updated": {"offerId": _text, "remaining": _integer, "pricePerEnergy": _number},
        "offer-withdrawn": {"offerId": _text},
        "bid": {"offerId": _text, "quantity": _integer},
        "bid-accepted": {
            "target": _uuid, "offerId": _text, "quantity": _integer,
            "pricePerEnergy": _number, "amount": _number, "swapId": _text,
            "swapToken": _text, "expiresAt": _instant,
        },
        "bid-rejected": {"target": _uuid, "offerId": _text},
        "request": {"ask": lambda value: isinstance(value, str) and value in {"status-statement", "distance-table"}},
        "negotiation-report": {"budgetBalance": _number, "energyBalance": _number},
        "ack": {"target": _uuid},
        "nack": {"target": _uuid},
    }
    _required(data, fields[message_type], message_type)

    if message_type in {"nack", "bid-rejected"}:
        codes = NACK_CODES if message_type == "nack" else BID_REJECTION_CODES
        reason = payload.get("reason")
        if not isinstance(reason, str) or codes.get(reason) != payload.get("code"):
            raise ValueError("reason/code no corresponde al tipo de rechazo v3")
    if message_type == "bid-rejected":
        if payload.get("reason") == "QUANTITY_UNAVAILABLE" and not _integer(data.get("available")):
            raise ValueError("QUANTITY_UNAVAILABLE requiere data.available entero")
        for field in ("available", "requested"):
            if field in data and not _integer(data[field]):
                raise ValueError(f"bid-rejected.data.{field} debe ser entero")
    if message_type in {"bid-rejected", "nack"} and "message" in data and not isinstance(data["message"], str):
        raise ValueError("data.message debe ser string")
    if message_type == "nack" and "cycleId" in data and (
        not isinstance(data["cycleId"], str) or len(data["cycleId"]) > 64
    ):
        raise ValueError("nack.data.cycleId debe tener máximo 64 caracteres")


def select_outbound_destination(
    item: dict,
    *,
    city_id: str,
    directed_exchange: str,
    market_exchange: str,
    central_routing_key: str,
) -> tuple[str, str]:
    """Devuelve el exchange y la ruta persistidos; rechaza cruces de destino."""
    payload = item.get("payload")
    message_type = item.get("type")
    if not isinstance(payload, dict) or payload.get("type") != message_type:
        raise ValueError("type y payload del outbox no coinciden")
    if str(payload.get("msgId")) != str(item.get("msgId")):
        raise ValueError("msgId y payload del outbox no coinciden")
    if str(payload.get("idpk")) != str(item.get("idpk")):
        raise ValueError("idpk y payload del outbox no coinciden")

    if message_type in V3_TYPES:
        if directed_exchange != "energy.x" or market_exchange != "market":
            raise ValueError("Exchanges v3 configurados no corresponden al contrato oficial")
        _validate_v3(payload, message_type, city_id)
    elif message_type not in LEGACY_DIRECTED_TYPES:
        raise ValueError(f"Tipo saliente no autorizado: {message_type}")

    exchange = item.get("exchange") or directed_exchange
    routing_key = item.get("routingKey")
    if message_type in MARKET_TYPES:
        expected_key = f"market.offer.{city_id}"
        if item.get("exchange") != market_exchange or routing_key != expected_key:
            raise ValueError("Oferta requiere exchange market y routing key propia persistidos")
        return market_exchange, expected_key

    if exchange != directed_exchange:
        raise ValueError("Mensaje dirigido requiere el exchange energy.x")
    if message_type in CITY_TYPES:
        if item.get("exchange") != directed_exchange:
            raise ValueError("Bid requiere exchange dirigido persistido")
        if not isinstance(routing_key, str) or not CITY_ROUTING_KEY.fullmatch(routing_key):
            raise ValueError("Bid requiere routing key city.{DESTINATARIO}")
    elif message_type in CENTRAL_TYPES:
        if routing_key != central_routing_key:
            raise ValueError("Mensaje a central requiere su routing key")
    elif message_type in RESPONSE_TYPES:
        if routing_key != central_routing_key and (
            not isinstance(routing_key, str) or not CITY_ROUTING_KEY.fullmatch(routing_key)
        ):
            raise ValueError("ACK/NACK requiere routing key de central o ciudad")
    elif routing_key is None:
        #Los outbox E1 antiguos no guardaban siempre una ruta explícita.
        routing_key = central_routing_key
    return directed_exchange, routing_key


def routing_key_for_directed_response(
    *, amqp_user_id: str | None, central_routing_key: str
) -> str | None:
    """Responde a la ciudad autenticada por AMQP, o a la central."""
    if isinstance(amqp_user_id, str) and CITY_ROUTING_KEY.fullmatch(amqp_user_id):
        return amqp_user_id
    if amqp_user_id == "central":
        return central_routing_key
    return None
