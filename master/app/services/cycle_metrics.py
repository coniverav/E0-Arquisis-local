"""E2-065 / RF06: métricas reproducibles por ciclo y estrategia AD6.

Nunca infiere compras desde bids aceptados ni usa el balance local como costo.
Los efectos contables confirmados de E2-051/052 son la fuente económica.
Las métricas se pueden recalcular tras redeliveries sin sumar dos veces.
No hace commit; la transacción pertenece al llamador.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_HALF_UP

from sqlmodel import Session, select

from ..models import (
    Cycle, CycleStrategyMetric, ImbalanceSettlement, LedgerEntry, MarketBid,
    StrategyConfig,
)

CENT = Decimal("0.01")
ZERO = Decimal("0")


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _baseline_rate(cycle: Cycle, settlements: list[ImbalanceSettlement]) -> tuple[Decimal | None, str | None]:
    """Prioriza la tarifa de liquidación recibida; si falta, estima desde status."""
    if settlements:
        return Decimal(settlements[-1].price_per_energy), "SETTLEMENT"
    market = (cycle.status_payload or {}).get("market")
    raw = market.get("imbalancePrice") if isinstance(market, dict) else None
    if raw is None or isinstance(raw, bool):
        return None, None
    try:
        rate = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return None, None
    if not rate.is_finite() or rate < 0:
        return None, None
    return rate, "STATUS_ESTIMATE"


def _buyer_swap(entry: LedgerEntry) -> bool:
    """Identifica compras confirmadas, no ventas ni otras entradas de energía."""
    details = entry.details or {}
    if not isinstance(details, dict):
        return False
    give = details.get("give")
    take = details.get("take")
    return (
        entry.operation_type == "SWAP_SETTLED"
        and isinstance(give, dict) and give.get("asset") == "CR"
        and isinstance(take, dict) and take.get("asset") == "KWH"
        and Decimal(entry.energy_delta) > 0
        and Decimal(entry.budget_delta) <= 0
    )


def refresh_cycle_metrics(
    session: Session, *, cycle_id: str, now: datetime | None = None,
) -> CycleStrategyMetric | None:
    """Inserta/actualiza una fila de RF06; bloquea el ciclo para serializar réplicas.

    La creación exige status confirmado y versión AD6 fijada. Nunca lee la
    configuración PENDING global. No crea migraciones ni altera el ledger.
    """
    now = _utc(now or datetime.now(timezone.utc))
    cycle = session.exec(
        select(Cycle).where(Cycle.cycle_id == cycle_id).with_for_update()
    ).first()
    if cycle is None or cycle.status_idpk is None or cycle.strategy_version is None:
        return None
    strategy = session.exec(select(StrategyConfig).where(
        StrategyConfig.version == cycle.strategy_version,
    )).first()
    if strategy is None:
        raise RuntimeError(f"STRATEGY_VERSION_NOT_FOUND: {cycle.strategy_version}")

    deficit = int(max(ZERO, Decimal(cycle.consumption or 0) - Decimal(
        cycle.generation_capacity or 0
    )).to_integral_value(rounding=ROUND_CEILING))

    bids = session.exec(select(MarketBid).where(MarketBid.cycle_id == cycle_id)).all()
    outgoing_rejected = sum(1 for b in bids if b.direction == "OUTGOING" and b.status == "REJECTED")
    incoming_rejected = sum(1 for b in bids if b.direction == "INCOMING" and b.status == "REJECTED")

    ledger = session.exec(select(LedgerEntry).where(LedgerEntry.cycle_id == cycle_id)).all()
    purchases = [entry for entry in ledger if _buyer_swap(entry)]
    bought = sum((Decimal(e.energy_delta) for e in purchases), ZERO)
    if bought != bought.to_integral_value():
        raise ValueError("SWAP_SETTLED buyer energyDelta debe ser KWH entero")
    bought_kwh = int(bought)
    covered = min(deficit, bought_kwh)

    # Costos desde el ledger confirmado: no asumir que una propuesta HTTP 202
    # equivale a swap-settled, ni usar el precio de un feed mutable.
    buy_cost = _money(-sum((Decimal(e.budget_delta) for e in purchases), ZERO))
    transport_entries = [e for e in ledger if e.operation_type == "SWAP_TRANSPORT"
                         and (e.details or {}).get("role") == "BUYER"]
    transport_cost = _money(-sum((Decimal(e.budget_delta) for e in transport_entries), ZERO))
    settlement_entries = [e for e in ledger if e.operation_type == "IMBALANCE_SETTLEMENT"]
    settlement_cost = (
        _money(-sum((Decimal(e.budget_delta) for e in settlement_entries), ZERO))
        if settlement_entries else None
    )

    # No declarar costo FINAL si hay compras confirmadas sin su cargo oficial
    # de transporte. Los IDs provienen del ledger y no de bids pendientes.
    purchase_ids = {str((e.details or {}).get("swapId") or e.idpk) for e in purchases}
    transport_ids = {str((e.details or {}).get("swapId") or e.idpk)
                     for e in transport_entries}
    missing_transport = len(purchase_ids - transport_ids)

    settlements = session.exec(select(ImbalanceSettlement).where(
        ImbalanceSettlement.cycle_id == cycle_id,
    ).order_by(ImbalanceSettlement.recorded_at, ImbalanceSettlement.id)).all()
    rate, baseline_source = _baseline_rate(cycle, settlements)
    baseline = _money(Decimal(deficit) * rate) if rate is not None else None

    closed = cycle.market_closes_at is not None and now >= _utc(cycle.market_closes_at)
    if not closed:
        state = "IN_PROGRESS"
    elif not settlements or not settlement_entries:
        state = "AWAITING_SETTLEMENT"
    elif missing_transport:
        state = "AWAITING_TRANSPORT"
    else:
        state = "FINAL"

    real_cost = _money(buy_cost + transport_cost + settlement_cost) if state == "FINAL" else None
    savings = _money(baseline - real_cost) if real_cost is not None and baseline is not None else None
    coverage_rate = (Decimal(covered) / Decimal(deficit)).quantize(
        Decimal("0.0001"), rounding=ROUND_HALF_UP,
    ) if deficit else Decimal("1.0000")

    values = {
        "strategy_version": strategy.version,
        "span_seconds": strategy.span_seconds,
        "selection_policy": strategy.offer_selection_policy,
        "acceptance_policy": strategy.bid_acceptance_policy,
        "deficit_kwh": deficit,
        "bought_kwh": bought_kwh,
        "covered_kwh": covered,
        "coverage_rate": coverage_rate,
        "rejected_bids": outgoing_rejected,
        "rejected_incoming_bids": incoming_rejected,
        "buy_cost": buy_cost,
        "transport_cost": transport_cost,
        "settlement_cost": settlement_cost,
        "real_cost": real_cost,
        "baseline_cost": baseline,
        "baseline_source": baseline_source,
        "savings_vs_baseline": savings,
        "missing_transport_trades": missing_transport,
        "status": state,
    }
    snapshot = session.get(CycleStrategyMetric, cycle_id)
    if snapshot is None:
        snapshot = CycleStrategyMetric(
            cycle_id=cycle_id, created_at=now, updated_at=now, **values,
        )
        session.add(snapshot)
    else:
        changed = any(getattr(snapshot, field) != value for field, value in values.items())
        if changed:
            for field, value in values.items():
                setattr(snapshot, field, value)
            snapshot.updated_at = now
            session.add(snapshot)
    session.flush()
    return snapshot


def refresh_recent_cycle_metrics(
    session: Session, *, now: datetime | None = None, limit: int = 12,
) -> int:
    """Respaldo del scheduler para cierres sin settlement y eventos tardíos."""
    now = _utc(now or datetime.now(timezone.utc))
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit debe ser un entero entre 1 y 100")
    cycles = session.exec(
        select(Cycle.cycle_id)
        .where(Cycle.status_idpk.is_not(None), Cycle.strategy_version.is_not(None),
               Cycle.market_opens_at.is_not(None),
               Cycle.market_opens_at <= now,
               Cycle.market_opens_at >= now - timedelta(minutes=90))
        .order_by(Cycle.market_opens_at.desc()).limit(limit)
    ).all()
    for cycle_id in cycles:
        refresh_cycle_metrics(session, cycle_id=cycle_id, now=now)
    return len(cycles)
