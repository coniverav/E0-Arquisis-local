"""E2-054: compra autónoma según déficit y capacidad de wallet.

El déficit se calcula con el status-statement del ciclo
(`consumption - generationCapacity`). La compra respeta:

- la estrategia y ventana del ciclo (`get_buy_candidates`);
- `minBid` y el `remaining` de cada oferta;
- un máximo de precio efectivo igual a `market.imbalancePrice`;
- la capacidad de wallet (`CR available + creditLine`);
- un solo bid activo por oferta/ciclo;
- un tope de bids por tick.

No se inventan saldos ni se reserva energía antes de que exista un bid. La
wallet sigue siendo la autoridad económica.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
import logging
import time
from typing import Any
from uuid import uuid4

from fastapi import HTTPException
from sqlmodel import Session, select

from app.config import AUTONOMOUS_BUY_INTERVAL_SECONDS
from app.database import engine
from app.models import Cycle, MarketBid
from app.services.buy_bids import enqueue_buy_bid
from app.services.offer_selection import get_buy_candidates
from app.services.wallet_client import WalletClient, WalletError

logger = logging.getLogger(__name__)

ACTIVE_BID_STATUSES = ("PENDING", "ACCEPTED", "RESERVED")
BUDGET_CONSUMING_STATUSES = ("PENDING",)
MAX_BIDS_PER_TICK = 5

_last_tick_at: float | None = None


def reset_autonomous_buy_gate() -> None:
    """Solo para pruebas: permite que el próximo tick se ejecute."""

    global _last_tick_at
    _last_tick_at = None


def _decimal(value: Any, default: str = "0") -> Decimal:
    if value is None:
        return Decimal(default)
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)
    return result if result.is_finite() else Decimal(default)


def _balance_value(balance: Any, name: str) -> Decimal:
    if isinstance(balance, dict):
        return _decimal(balance.get(name))
    return _decimal(getattr(balance, name, "0"))


def _deficit(cycle: Cycle) -> int:
    consumption = _decimal(cycle.consumption)
    generation = _decimal(cycle.generation_capacity)
    return int(max(Decimal("0"), consumption - generation).to_integral_value(
        rounding=ROUND_CEILING
    ))


def _imbalance_price(cycle: Cycle) -> Decimal | None:
    payload = cycle.status_payload or {}
    market = payload.get("market") if isinstance(payload, dict) else None
    if not isinstance(market, dict) or market.get("imbalancePrice") is None:
        return None
    price = _decimal(market.get("imbalancePrice"))
    return price if price >= 0 else None


async def autonomous_buy_tick(
    session: Session,
    *,
    cycle_id: str,
    now: datetime | None = None,
    client: WalletClient | None = None,
    max_bids: int = MAX_BIDS_PER_TICK,
) -> dict[str, int]:
    """Emite bids de compra para cubrir el déficit del ciclo.

    El caller decide el commit; `enqueue_buy_bid` confirma cada bid de forma
    durable (outbox transaccional) para no perder intenciones.
    """

    now = (
        now.replace(tzinfo=timezone.utc)
        if now is not None and now.tzinfo is None
        else (now or datetime.now(timezone.utc))
    )
    if now.tzinfo is None:
        raise ValueError("now requiere zona horaria")

    stats = {
        "deficit": 0,
        "bids_created": 0,
        "quantity_bid": 0,
        "skipped_price": 0,
        "skipped_capacity": 0,
        "skipped_race": 0,
    }

    cycle = session.get(Cycle, cycle_id)
    if cycle is None:
        return stats

    deficit = _deficit(cycle)
    stats["deficit"] = deficit
    if deficit <= 0:
        return stats

    candidates = get_buy_candidates(session, cycle_id=cycle_id, now=now)
    if not candidates.canBid or candidates.minBid is None:
        return stats

    price_cap = _imbalance_price(cycle)
    if price_cap is None:
        logger.warning("Ciclo %s sin imbalancePrice; no se compra", cycle_id)
        return stats

    client = client or WalletClient()
    try:
        balance = await client.get_balance()
    except WalletError as exc:
        logger.warning("Wallet no disponible para compra autónoma: %s", exc)
        return stats

    capacity = (
        _balance_value(balance, "cr_available")
        + _balance_value(balance, "credit_line")
    )
    if capacity < 0:
        capacity = Decimal("0")

    active_bids = session.exec(
        select(MarketBid).where(
            MarketBid.cycle_id == cycle_id,
            MarketBid.direction == "OUTGOING",
            MarketBid.status.in_(ACTIVE_BID_STATUSES),
        )
    ).all()

    remaining_deficit = deficit - sum(bid.quantity for bid in active_bids)
    active_offers = {bid.offer_id for bid in active_bids}

    candidate_by_offer = {
        item.offerId: item for item in candidates.candidates
    }
    for bid in active_bids:
        if bid.status not in BUDGET_CONSUMING_STATUSES:
            continue
        candidate = candidate_by_offer.get(bid.offer_id)
        price = (
            _decimal(candidate.effectivePricePerEnergy)
            if candidate is not None
            and candidate.effectivePricePerEnergy is not None
            else price_cap
        )
        capacity -= Decimal(bid.quantity) * price

    if remaining_deficit <= 0:
        return stats

    for candidate in candidates.candidates:
        if stats["bids_created"] >= max_bids or remaining_deficit <= 0:
            break
        if candidate.offerId in active_offers:
            continue
        if candidate.effectivePricePerEnergy is None:
            continue

        price = _decimal(candidate.effectivePricePerEnergy)
        if price > price_cap:
            stats["skipped_price"] += 1
            continue
        if candidate.remaining is None or candidate.remaining < candidates.minBid:
            continue

        quantity = min(remaining_deficit, candidate.remaining)
        if quantity < candidates.minBid:
            continue

        if price > 0 and Decimal(quantity) * price > capacity:
            affordable = int((capacity / price).to_integral_value(
                rounding=ROUND_FLOOR
            ))
            quantity = min(quantity, affordable)
            if quantity < candidates.minBid:
                stats["skipped_capacity"] += 1
                continue

        try:
            enqueue_buy_bid(
                session,
                cycle_id=cycle_id,
                offer_id=candidate.offerId,
                quantity=quantity,
                idpk=uuid4(),
                now=now,
            )
        except HTTPException as exc:
            logger.info(
                "Bid autónomo descartado (%s): %s",
                candidate.offerId,
                exc.detail,
            )
            stats["skipped_race"] += 1
            continue

        stats["bids_created"] += 1
        stats["quantity_bid"] += quantity
        remaining_deficit -= quantity
        capacity -= Decimal(quantity) * price
        active_offers.add(candidate.offerId)

    return stats


async def run_autonomous_buy_tick(
    *,
    now: datetime | None = None,
    client: WalletClient | None = None,
    force: bool = False,
) -> dict[str, int]:
    """Entrada del scheduler, con intervalo mínimo entre consultas."""

    global _last_tick_at

    monotonic_now = time.monotonic()
    if (
        not force
        and _last_tick_at is not None
        and monotonic_now - _last_tick_at < AUTONOMOUS_BUY_INTERVAL_SECONDS
    ):
        return {"bids_created": 0, "skipped_interval": 1}

    _last_tick_at = monotonic_now
    now = now or datetime.now(timezone.utc)
    client = client or WalletClient()

    with Session(engine) as session:
        cycle_ids = session.exec(
            select(Cycle.cycle_id)
            .where(Cycle.status_idpk.is_not(None))
            .where(Cycle.market_opens_at.is_not(None))
            .where(Cycle.market_opens_at <= now)
            .where(Cycle.market_last_bid_at > now)
            .where(Cycle.generation_capacity < Cycle.consumption)
            .order_by(Cycle.market_opens_at.desc())
            .limit(5)
        ).all()

    totals = {"bids_created": 0, "cycles": 0}
    for cycle_id in cycle_ids:
        try:
            with Session(engine) as session:
                stats = await autonomous_buy_tick(
                    session, cycle_id=cycle_id, now=now, client=client,
                )
                session.commit()
            totals["bids_created"] += stats["bids_created"]
            totals["cycles"] += 1
        except Exception:
            logger.exception("Error en compra autónoma del ciclo %s", cycle_id)

    return totals
