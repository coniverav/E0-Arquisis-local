"""Contrato puro del input seller para StartExecution (E3-027 / ADR7-ADR8).

No requiere PostgreSQL, boto3 ni llamadas remotas. La operación queda
identificada por el idpk de bid; los importes se leen de SwapRecord, nunca
se recalculan con la oferta actual.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import NAMESPACE_URL, uuid5


def _iso8601(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("MARKET_CLOSES_AT_REQUIRED")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def build_seller_execution_input(
    *,
    bid_idpk: str,
    bid_msg_id: str,
    cycle_id: str,
    offer_id: str,
    buyer_city_id: str,
    quantity_kwh: int,
    give_amount: Decimal,
    take_amount: Decimal,
    wallet_idempotency_key: str,
    market_closes_at: datetime,
) -> dict[str, Any]:
    """Datos autosuficientes, sin secretos, para la saga Standard seller.

    Cantidades monetarias son cadenas decimales exactas; no float.
    La saga posterior verifica closings y puede compensar un inicio tardío.
    """
    if not all(isinstance(s, str) and s for s in (
        bid_idpk, bid_msg_id, cycle_id, offer_id, buyer_city_id, wallet_idempotency_key
    )):
        raise ValueError("SELLER_IDENTITY_REQUIRED")
    if wallet_idempotency_key != bid_idpk:
        raise ValueError("SELLER_IDEMPOTENCY_KEY_MISMATCH")
    if isinstance(quantity_kwh, bool) or not isinstance(quantity_kwh, int) or quantity_kwh <= 0:
        raise ValueError("SELLER_INVALID_KWH")
    give = Decimal(str(give_amount))
    take = Decimal(str(take_amount))
    if (not give.is_finite() or not take.is_finite() or give != quantity_kwh
            or take < 0 or take != take.quantize(Decimal("0.01"))):
        raise ValueError("SELLER_INVALID_SWAP_AMOUNTS")
    closes_at = _iso8601(market_closes_at)
    # Una identidad de cancelación estable para el futuro adaptador Wallet:
    # no reutilizar la llave del POST /swaps ni inventar una en cada retry.
    cancel_key = str(uuid5(NAMESPACE_URL, f"energyshark:seller:cancel:{bid_idpk}"))
    return {
        "schemaVersion": 1,
        "role": "SELLER",
        "idpk": bid_idpk,
        "cycleId": cycle_id,
        "marketClosesAt": closes_at,
        # Contrato plano consumido por los JSONPath de seller.asl.json.
        # sagaExecutionId es un idpk: PostgresStore._lock_execution permite
        # resolver esa identidad incluso antes del ACK de StartExecution.
        "sagaExecutionId": bid_idpk,
        "offerId": offer_id,
        "quantity": quantity_kwh,
        "timeoutAt": _iso8601(market_closes_at + timedelta(seconds=60)),
        "timeouts": {"proposalSeconds": 30, "terminalSeconds": 30},
        "bid": {
            "idpk": bid_idpk,
            "msgId": bid_msg_id,
            "offerId": offer_id,
            "buyerCityId": buyer_city_id,
            "quantityKwh": quantity_kwh,
        },
        "swap": {
            "idpk": bid_idpk,
            "idempotencyKey": wallet_idempotency_key,
            "cancelIdempotencyKey": cancel_key,
            "give": {"asset": "KWH", "amount": str(give)},
            "take": {"asset": "CR", "amount": f"{take:.2f}"},
            "memo": offer_id,
        },
    }
