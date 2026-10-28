"""E2-050: financiamiento v3 estricto.

El `transfer` v3 de la central es *solo financiamiento*: trae el monto neto
en `quantity` y los datos oficiales de la wallet en `balanceAfter` y
`creditLine`. La multa (`penalty`) es informativa: ya viene descontada del
neto y **no** se vuelve a debitar.

La wallet sigue siendo la autoridad económica. Si `balanceAfter` no coincide
con el saldo local, la diferencia se registra en `details.funding` para que
E2-044 la explique; nunca se corrige el ledger en silencio.

El `transfer` E1 con `becauseOf` (pago de negociación) no pasa por este
módulo.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from sqlmodel import Session


INVALID_FUNDING_V3 = "INVALID_FUNDING_V3"

_REQUIRED_V3_FIELDS = ("balanceAfter", "creditLine")


def _as_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not result.is_finite():
        return None
    return result


def validate_funding_v3(data: dict[str, Any] | None) -> str | None:
    """Devuelve un motivo de rechazo o ``None`` si el funding v3 es válido.

    Reglas:
    - `quantity` numérico y finito.
    - `balanceAfter` y `creditLine` obligatorios y numéricos.
    - `creditLine >= 0`.
    - `penalty` es informativo: se conserva, nunca se vuelve a debitar.
    """

    if not isinstance(data, dict):
        return INVALID_FUNDING_V3

    quantity = _as_decimal(data.get("quantity"))
    if quantity is None:
        return INVALID_FUNDING_V3

    for field in _REQUIRED_V3_FIELDS:
        if field not in data or data.get(field) is None:
            return INVALID_FUNDING_V3
        if _as_decimal(data.get(field)) is None:
            return INVALID_FUNDING_V3

    if _as_decimal(data["creditLine"]) < 0:
        return INVALID_FUNDING_V3

    # `penalty` es informativo y ya viene descontado de `quantity`. Se
    # conserva tal como llegó (también en el envelope E1) sin volver a
    # debitar ni bloquear el financiamiento por su forma.
    return None


def record_funding_audit(
    session: Session,
    *,
    entry,
    payload,
) -> None:
    """Agrega a `details.funding` la comparación con la wallet.

    No modifica el efecto del ledger: `entry` ya se aplicó con `quantity`
    como monto neto. Solo deja evidencia durable de la comparación.
    """

    data = payload.data or {}
    balance_after = _as_decimal(data.get("balanceAfter"))
    credit_line = _as_decimal(data.get("creditLine"))

    details = dict(entry.details or {})
    details["funding"] = {
        "quantity": str(data.get("quantity")),
        "balanceAfter": str(balance_after),
        "creditLine": str(credit_line),
        "penalty": data.get("penalty"),
        "localBalanceAfter": str(entry.budget_after),
        "balanceDifference": str(entry.budget_after - balance_after),
    }

    entry.details = details
    session.add(entry)
    session.flush()
