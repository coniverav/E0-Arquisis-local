"""E2-051: efectos de cierre de mercado sobre el ledger E1.

Reglas locales:

- El ledger E1 explica la economía del ciclo; la wallet sigue siendo la
  autoridad de los saldos.
- KWH local debe quedar en 0 al cierre. El `kwhHeld` del settlement y los
  déficits se explican en `details`, no se copian a `Cycle.energy_balance`.
- Un `swap-settled` aplica el intercambio una sola vez por `swapId`.
- Un `trade-recorded` aplica transporte solo al comprador, una vez por
  `swapId`.
- Un `imbalance-settlement` aplica su `total` una sola vez por
  `settlementId` y luego extingue la energía local restante.
- Si falta el settlement al cierre, se extingue la energía local igualmente
  con un asiento de fallback. Las diferencias de presupuesto se registran
  para E2-044; no se corrigen en silencio.
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal

from sqlalchemy import and_, or_
from sqlmodel import Session, select

from app.models import Cycle, ImbalanceSettlement, LedgerEntry, SwapRecord, TradeRecord
from app.services.ledger import apply_ledger_effect

logger = logging.getLogger(__name__)

SUPPORTED_ASSETS = {"KWH", "CR"}


def _asset_effects(record: SwapRecord) -> tuple[Decimal, Decimal] | None:
    budget_delta = Decimal("0")
    energy_delta = Decimal("0")
    assets = (record.give_asset, record.take_asset)
    if any(asset not in SUPPORTED_ASSETS for asset in assets):
        return None

    for asset, amount, sign in (
        (record.give_asset, record.give_amount, Decimal("-1")),
        (record.take_asset, record.take_amount, Decimal("1")),
    ):
        if asset == "KWH":
            energy_delta += sign * Decimal(amount)
        else:
            budget_delta += sign * Decimal(amount)
    return budget_delta, energy_delta


def apply_swap_settled(
    session: Session,
    *,
    record: SwapRecord,
    source_msg_id: str | None = None,
) -> tuple[LedgerEntry, bool] | None:
    """Aplica el intercambio confirmado por wallet una vez por `swapId`."""

    if session.get(Cycle, record.cycle_id) is None:
        logger.warning(
            "swap-settled sin Cycle local; no se aplica ledger (swapId=%s)",
            record.swap_id,
        )
        return None

    effects = _asset_effects(record)
    if effects is None:
        logger.warning(
            "swap-settled con activos no soportados; no se aplica ledger "
            "(swapId=%s, give=%s, take=%s)",
            record.swap_id,
            record.give_asset,
            record.take_asset,
        )
        return None

    budget_delta, energy_delta = effects
    idpk = record.swap_id or record.idpk
    return apply_ledger_effect(
        session,
        cycle_id=record.cycle_id,
        idpk=idpk,
        source_msg_id=source_msg_id,
        operation_type="SWAP_SETTLED",
        budget_delta=budget_delta,
        energy_delta=energy_delta,
        details={
            "swapId": record.swap_id,
            "role": record.role,
            "counterpartCityId": record.counterpart_city_id,
            "give": {
                "asset": record.give_asset,
                "amount": str(record.give_amount),
            },
            "take": {
                "asset": record.take_asset,
                "amount": str(record.take_amount),
            },
            "source": "swap-settled",
        },
    )


def apply_trade_transport(
    session: Session,
    *,
    record: TradeRecord,
) -> tuple[LedgerEntry, bool] | None:
    """Aplica el costo de transporte solo al comprador, una vez por swap."""

    if record.role != "BUYER":
        return None

    if session.get(Cycle, record.cycle_id) is None:
        logger.warning(
            "trade-recorded sin Cycle local; se persiste pero no se aplica "
            "ledger (swapId=%s)",
            record.swap_id,
        )
        return None

    return apply_ledger_effect(
        session,
        cycle_id=record.cycle_id,
        idpk=record.swap_id,
        source_msg_id=record.msg_id,
        operation_type="SWAP_TRANSPORT",
        budget_delta=-Decimal(record.transport_total),
        energy_delta=Decimal("0"),
        details={
            "swapId": record.swap_id,
            "offerId": record.offer_id,
            "role": record.role,
            "counterpartCityId": record.counterpart_city_id,
            "transportPerEnergy": str(record.transport_per_energy),
            "transportTotal": str(record.transport_total),
            "routeEnabled": record.route_enabled,
            "source": "trade-recorded",
        },
    )


def apply_imbalance_settlement(
    session: Session,
    *,
    settlement: ImbalanceSettlement,
) -> tuple[LedgerEntry, bool] | None:
    """Aplica el settlement una vez y extingue la energía local restante."""

    cycle = session.get(Cycle, settlement.cycle_id)
    if cycle is None:
        logger.warning(
            "imbalance-settlement sin Cycle local; se persiste pero no se "
            "aplica ledger (settlementId=%s)",
            settlement.settlement_id,
        )
        return None

    local_energy_before = Decimal(cycle.energy_balance)
    local_budget_before = Decimal(cycle.budget_balance)

    entry = apply_ledger_effect(
        session,
        cycle_id=settlement.cycle_id,
        idpk=settlement.settlement_id,
        source_msg_id=settlement.msg_id,
        operation_type="IMBALANCE_SETTLEMENT",
        budget_delta=Decimal(settlement.total),
        energy_delta=Decimal("0"),
        details={
            "settlementId": settlement.settlement_id,
            "kwhHeld": settlement.kwh_held,
            "deficit": settlement.deficit,
            "coverage": settlement.coverage,
            "pricePerEnergy": str(settlement.price_per_energy),
            "total": str(settlement.total),
            "balanceAfter": str(settlement.balance_after),
            "localEnergyBefore": str(local_energy_before),
            "localBudgetBefore": str(local_budget_before),
            "localBudgetAfter": str(local_budget_before + Decimal(settlement.total)),
            "balanceDifference": str(
                local_budget_before + Decimal(settlement.total)
                - Decimal(settlement.balance_after)
            ),
            "source": "imbalance-settlement",
        },
    )

    # La energía local se extingue al cierre. Si el settlement ya la dejó en
    # 0 no se agrega un segundo asiento.
    if cycle.energy_balance != 0:
        apply_ledger_effect(
            session,
            cycle_id=settlement.cycle_id,
            idpk=settlement.settlement_id,
            source_msg_id=settlement.msg_id,
            operation_type="IMBALANCE_BURN",
            budget_delta=Decimal("0"),
            energy_delta=-Decimal(cycle.energy_balance),
            details={
                "settlementId": settlement.settlement_id,
                "reason": "KWH_LOCAL_MUST_BE_ZERO_AT_CLOSE",
                "energyBefore": str(cycle.energy_balance),
                "kwhHeld": settlement.kwh_held,
                "deficit": settlement.deficit,
                "coverage": settlement.coverage,
            },
        )

    return entry


def finalize_closed_markets(
    session: Session,
    *,
    now: datetime,
) -> dict[str, int]:
    """Garantiza KWH local 0 para ciclos cuyo mercado ya cerró.

    Cubre dos momentos: `market.closesAt` y la apertura de la ventana de
    reporte. Si el settlement no llegó, se usa un asiento explícito de
    fallback para no dejar energía viva que distorsione el reporte.
    """

    cycles = session.exec(
        select(Cycle)
        .where(Cycle.status_idpk.is_not(None))
        .where(
            or_(
                and_(
                    Cycle.market_closes_at.is_not(None),
                    Cycle.market_closes_at <= now,
                ),
                and_(
                    Cycle.report_window_opens_at.is_not(None),
                    Cycle.report_window_opens_at <= now,
                ),
            )
        )
        .order_by(Cycle.cycle_id)
        .with_for_update(skip_locked=True)
    ).all()

    examined = 0
    burns_applied = 0

    for cycle in cycles:
        examined += 1
        if Decimal(cycle.energy_balance) == 0:
            continue

        settlement_exists = session.exec(
            select(ImbalanceSettlement.id).where(
                ImbalanceSettlement.cycle_id == cycle.cycle_id
            )
        ).first() is not None

        operation_type = (
            "IMBALANCE_CLOSE" if settlement_exists else "IMBALANCE_BURN_FALLBACK"
        )
        idpk = (
            f"close:{cycle.cycle_id}"
            if settlement_exists
            else f"fallback:{cycle.cycle_id}"
        )
        apply_ledger_effect(
            session,
            cycle_id=cycle.cycle_id,
            idpk=idpk,
            source_msg_id=None,
            operation_type=operation_type,
            budget_delta=Decimal("0"),
            energy_delta=-Decimal(cycle.energy_balance),
            details={
                "reason": (
                    "LATE_SETTLEMENT" if settlement_exists else "SETTLEMENT_MISSING"
                ),
                "energyBefore": str(cycle.energy_balance),
                "marketClosedAt": (
                    cycle.market_closes_at.isoformat()
                    if cycle.market_closes_at
                    else None
                ),
                "reportWindowOpensAt": (
                    cycle.report_window_opens_at.isoformat()
                    if cycle.report_window_opens_at
                    else None
                ),
            },
        )
        burns_applied += 1

    return {"cycles_examined": examined, "burns_applied": burns_applied}
