"""E2-043: hechos `trade-recorded` e `imbalance-settlement` de la central.

Ambos mensajes son hechos consumados: la central no pide confirmación. Se
persisten con identidad de negocio (`swapId` / `settlementId`), no con la
identidad del envelope, para que un reenvío con otro `idpk`/`msgId` no
duplique transporte ni el cargo de desbalance.

No se toca `Cycle.budget_balance` (ledger E1): estas tablas explican el
efecto económico y alimentan la conciliación de E2-044, mientras la wallet
sigue siendo la autoridad de los saldos.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlmodel import Session, select

from app.config import CITY_ID
from app.models import ImbalanceSettlement, TradeRecord
from app.services.message_audit import (
    INBOUND_DUPLICATE,
    INBOUND_NOT_PROCESSED,
    INBOUND_PROCESSED,
)


def _decimal(value: Any) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError("valor numérico requerido")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("valor numérico inválido") from exc
    if not result.is_finite():
        raise ValueError("valor numérico inválido")
    return result


def _int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("entero requerido")
    return value


def _str(value: Any, *, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError("string no vacío requerido")
    return value


def process_trade_recorded(
    session: Session,
    *,
    payload,
    now: datetime,
) -> tuple[str, str | None]:
    """Registra el transporte de un swap una sola vez por `swapId`."""

    data = payload.data or {}
    if not payload.cycleId:
        return INBOUND_NOT_PROCESSED, "MISSING_CYCLE"
    try:
        swap_id = _str(data.get("swapId"))
        offer_id = _str(data.get("offerId"), allow_none=True)
        seller = _str(data.get("seller"))
        buyer = _str(data.get("buyer"))
        quantity = _int(data.get("quantity"))
        if quantity <= 0:
            raise ValueError("quantity debe ser positiva")
        price = _decimal(data.get("pricePerEnergy"))
        energy_total = _decimal(data.get("energyTotal"))
        transport_per_energy = _decimal(data.get("transportPerEnergy"))
        transport_total = _decimal(data.get("transportTotal"))
        route_enabled = data.get("routeEnabled")
        if not isinstance(route_enabled, bool):
            raise ValueError("routeEnabled debe ser booleano")
    except ValueError:
        return INBOUND_NOT_PROCESSED, "INVALID_TRADE_RECORDED"

    if CITY_ID == seller:
        role, counterpart = "SELLER", buyer
    elif CITY_ID == buyer:
        role, counterpart = "BUYER", seller
    else:
        return INBOUND_NOT_PROCESSED, "TRADE_NOT_ADDRESSED_TO_US"

    existing = session.exec(
        select(TradeRecord).where(TradeRecord.swap_id == swap_id)
    ).first()
    if existing is not None:
        if (
            existing.seller == seller
            and existing.buyer == buyer
            and existing.quantity == quantity
            and existing.transport_total == transport_total
        ):
            return INBOUND_DUPLICATE, None
        return INBOUND_NOT_PROCESSED, "TRADE_CONFLICT"

    session.add(TradeRecord(
        idpk=str(payload.idpk),
        msg_id=str(payload.msgId),
        cycle_id=payload.cycleId,
        swap_id=swap_id,
        offer_id=offer_id,
        seller=seller,
        buyer=buyer,
        role=role,
        counterpart_city_id=counterpart,
        quantity=quantity,
        price_per_energy=price,
        energy_total=energy_total,
        transport_per_energy=transport_per_energy,
        transport_total=transport_total,
        route_enabled=route_enabled,
        recorded_at=now,
    ))
    session.flush()
    return INBOUND_PROCESSED, None


def process_imbalance_settlement(
    session: Session,
    *,
    payload,
    now: datetime,
) -> tuple[str, str | None]:
    """Registra el cierre de mercado una sola vez por `settlementId`."""

    data = payload.data or {}
    if not payload.cycleId:
        return INBOUND_NOT_PROCESSED, "MISSING_CYCLE"
    try:
        settlement_id = _str(data.get("settlementId"))
        kwh_held = _int(data.get("kwhHeld"))
        deficit = _int(data.get("deficit"))
        coverage = _int(data.get("coverage"))
        price = _decimal(data.get("pricePerEnergy"))
        total = _decimal(data.get("total"))
        balance_after = _decimal(data.get("balanceAfter"))
    except ValueError:
        return INBOUND_NOT_PROCESSED, "INVALID_IMBALANCE_SETTLEMENT"

    if min(kwh_held, deficit) < 0:
        return INBOUND_NOT_PROCESSED, "INVALID_IMBALANCE_SETTLEMENT"
    if coverage != kwh_held - deficit:
        return INBOUND_NOT_PROCESSED, "IMBALANCE_COVERAGE_MISMATCH"
    expected_total = (Decimal(coverage) * price).quantize(Decimal("0.01"))
    if total.quantize(Decimal("0.01")) != expected_total:
        return INBOUND_NOT_PROCESSED, "IMBALANCE_TOTAL_MISMATCH"

    existing = session.exec(
        select(ImbalanceSettlement).where(
            ImbalanceSettlement.settlement_id == settlement_id
        )
    ).first()
    if existing is not None:
        if (
            existing.coverage == coverage
            and existing.total == total
            and existing.balance_after == balance_after
        ):
            return INBOUND_DUPLICATE, None
        return INBOUND_NOT_PROCESSED, "IMBALANCE_CONFLICT"

    session.add(ImbalanceSettlement(
        idpk=str(payload.idpk),
        msg_id=str(payload.msgId),
        cycle_id=payload.cycleId,
        settlement_id=settlement_id,
        kwh_held=kwh_held,
        deficit=deficit,
        coverage=coverage,
        price_per_energy=price,
        total=total,
        balance_after=balance_after,
        recorded_at=now,
    ))
    session.flush()
    return INBOUND_PROCESSED, None


def process_wallet_fact(
    session: Session,
    *,
    payload,
    now: datetime,
) -> tuple[str, str | None]:
    """Despacha los hechos de wallet que no son eventos swap-*."""

    if payload.type == "trade-recorded":
        return process_trade_recorded(session, payload=payload, now=now)
    if payload.type == "imbalance-settlement":
        return process_imbalance_settlement(session, payload=payload, now=now)
    raise ValueError(f"tipo de hecho wallet no soportado: {payload.type}")
