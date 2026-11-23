"""E3-044: input inmutable y sin JWT para la saga Standard del comprador.

El token de Wallet se guarda en SwapRecord (y el inbox auditado); no viaja en el input de
StartExecution, logs o resultados de tareas Step Functions.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any


def _utc(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"BUYER_{label}_TIMEZONE_REQUIRED")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def build_buyer_execution_input(
    *, acceptance_idpk: str, bid_idpk: str, bid_msg_id: str, cycle_id: str,
    offer_id: str, seller_city_id: str, quantity_kwh: int,
    amount_cr: Decimal, swap_id: str, accept_idempotency_key: str,
    expires_at: datetime, closes_at: datetime, swap_record_id: int,
    now: datetime,
) -> dict[str, Any]:
    """Congela la identidad de E3 sin exponer swapToken en AWS.

    El nombre de ejecución es el idpk de *bid-accepted*, no el idpk del bid
    saliente. El plazo terminal deja 60 s para registrar resultado tardío.
    """
    if not all(isinstance(x, str) and bool(x) for x in (
        acceptance_idpk, bid_idpk, bid_msg_id, cycle_id, offer_id,
        seller_city_id, swap_id, accept_idempotency_key,
    )):
        raise ValueError("BUYER_SAGA_IDENTITY_REQUIRED")
    if accept_idempotency_key != acceptance_idpk:
        raise ValueError("BUYER_SAGA_IDEMPOTENCY_MISMATCH")
    if type(quantity_kwh) is not int or quantity_kwh <= 0:
        raise ValueError("BUYER_SAGA_QUANTITY_INVALID")
    if type(swap_record_id) is not int or swap_record_id <= 0:
        raise ValueError("BUYER_SAGA_SWAP_RECORD_REQUIRED")
    try:
        amount = Decimal(str(amount_cr))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("BUYER_SAGA_AMOUNT_INVALID") from exc
    if not amount.is_finite() or amount < 0 or amount != amount.quantize(Decimal("0.01")):
        raise ValueError("BUYER_SAGA_AMOUNT_INVALID")
    expires = _utc(expires_at, "EXPIRY")
    closes = _utc(closes_at, "CLOSE")
    moment = _utc(now, "NOW")
    # Aun si la aceptación llegó vencida, iniciar una saga que pueda
    # rechazarla explícitamente. No inferir que HTTP 202 la liquidó.
    deadline = max(moment, min(expires, closes)) + timedelta(seconds=60)
    return {
        "schemaVersion": 1,
        "role": "BUYER",
        "idpk": acceptance_idpk,
        "sagaExecutionId": acceptance_idpk,
        "cycleId": cycle_id,
        "swapRecordId": swap_record_id,
        "closesAt": _iso(closes),
        "timeoutAt": _iso(deadline),
        "timeouts": {"minimumRemainingSeconds": 3, "acceptSeconds": 30},
        "bid": {
            "idpk": bid_idpk,
            "msgId": bid_msg_id,
            "offerId": offer_id,
            "sellerCityId": seller_city_id,
            "quantityKwh": quantity_kwh,
        },
        "expectedClaims": {
            "proposer": seller_city_id,
            "give": {"asset": "KWH", "amount": str(quantity_kwh)},
            "take": {"asset": "CR", "amount": f"{amount:.2f}"},
            "memo": offer_id,
        },
        "swap": {
            "swapId": swap_id,
            "acceptIdempotencyKey": accept_idempotency_key,
            "expiresAt": _iso(expires),
        },
    }
