"""E3-028 / ADR-008: intenciones de oferta y reporte v3 en un outbox único.

No publica mensajes, no llama a AWS, no hace commit y no regenera IDs. El
productor genera el envelope una vez y el llamador confirma negocio + outbox
mediante el mismo commit PostgreSQL.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from sqlmodel import Session

from ..models import OutboundMessage
from .outbox_contract import validate_broker_envelope
from .outbox_store import enqueue_broker

OFFER_TYPES = frozenset({"offer", "offer-updated", "offer-withdrawn"})
CRITICAL_BROKER_TYPES = OFFER_TYPES | {"negotiation-report"}


def enqueue_market_envelope(
    session: Session,
    *,
    envelope: Mapping[str, Any],
    exchange_name: str,
    routing_key: str,
    available_at: datetime | None,
    expires_at: datetime | None,
    now: datetime | None = None,
) -> OutboundMessage:
    """Persiste EXACTAMENTE el envelope v3 definido por el productor.

    - offer* -> exchange market y market.offer.<ciudad-local>
    - negotiation-report -> exchange energy.x (clave central parametrizada)
    - el idpk NO es único por fila: un reintento de negocio de un reporte
      usa otro msgId manteniendo el idpk del reporte; el relay NO lo altera.
    """
    payload = validate_broker_envelope(envelope)
    kind = payload["type"]
    if kind not in CRITICAL_BROKER_TYPES:
        raise ValueError("E3028_UNSUPPORTED_MARKET_MESSAGE_TYPE")
    if (not isinstance(payload.get("timestamp"), str) or not payload["timestamp"]
            or not isinstance(payload.get("cityId"), str) or not payload["cityId"]
            or not isinstance(payload.get("data"), dict)):
        raise ValueError("E3028_INVALID_V3_ENVELOPE")

    if kind in OFFER_TYPES:
        expected = f"market.offer.{payload['cityId']}"
        if exchange_name != "market" or routing_key != expected:
            raise ValueError("E3028_INVALID_OFFER_DESTINATION")
        if not isinstance(payload["data"].get("offerId"), str) or not payload["data"]["offerId"]:
            raise ValueError("E3028_OFFER_ID_REQUIRED")
    else:
        if exchange_name != "energy.x" or not routing_key or not routing_key.strip():
            raise ValueError("E3028_INVALID_REPORT_DESTINATION")
        if not {"budgetBalance", "energyBalance"}.issubset(payload["data"]):
            raise ValueError("E3028_REPORT_BALANCES_REQUIRED")

    return enqueue_broker(
        session,
        envelope=payload,
        exchange_name=exchange_name,
        routing_key=routing_key,
        available_at=available_at,
        expires_at=expires_at,
        now=now,
    )
