"""E3-027: reservar (AD4) + intención StartExecution en el MISMO commit.

Invocado sólo en la transición PENDING -> RESERVED, en la misma Session que
OfferReservation(HOLD), SwapRecord(SUBMITTED) y MarketBid(RESERVED).
No hace commit y NO llama AWS ni Wallet. El flag queda apagado por defecto.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os

from sqlmodel import Session, select

from ..models import Cycle, MarketBid, OfferReservation, OutboundMessage, SwapRecord
from .outbox_store import enqueue_saga_start
from .saga_tokens import get_or_create_saga_execution
from .seller_saga_intent import build_seller_execution_input


def seller_saga_enabled() -> bool:
    """Leído por operación para despliegue gradual y pruebas aisladas."""
    return os.getenv("E3_SELLER_SAGA_ENABLED", "false").strip().lower() in {
        "1", "true", "yes", "on"
    }


def _seller_machine_arn() -> str:
    arn = os.getenv("E3_SELLER_STATE_MACHINE_ARN", "").strip()
    if not arn or not arn.startswith("arn:aws:states:"):
        raise ValueError("E3_SELLER_STATE_MACHINE_ARN_REQUIRED")
    return arn


def enqueue_seller_saga_on_reserve(
    session: Session, *, bid_idpk: str, now: datetime,
) -> OutboundMessage | None:
    """Encola un inicio para cada NUEVA reserva, sin alterar ruta E2 apagada.

    Si el ARN no está configurado cuando el flag está activo, genera error:
    el caller debe hacer rollback completo, evitando HOLD huérfano.
    """
    if not seller_saga_enabled():
        return None
    arn = _seller_machine_arn()  # fail-fast antes de construir la intención
    bid = session.exec(select(MarketBid).where(
        MarketBid.idpk == bid_idpk,
        MarketBid.direction == "INCOMING",
    )).one()
    swap = session.exec(select(SwapRecord).where(
        SwapRecord.idpk == bid_idpk,
        SwapRecord.role == "PROPOSER",
    )).one()
    cycle = session.get(Cycle, bid.cycle_id)
    hold = session.exec(select(OfferReservation).where(
        OfferReservation.bid_idpk == bid_idpk,
        OfferReservation.status == "HOLD",
    )).one()
    if (bid.status != "RESERVED" or cycle is None
            or cycle.market_closes_at is None
            or hold.offer_id != bid.offer_id or hold.quantity != bid.quantity
            or swap.cycle_id != bid.cycle_id or swap.memo != bid.offer_id
            or swap.counterpart_city_id != bid.counterpart_city_id
            or swap.status != "SUBMITTED" or swap.give_asset != "KWH"
            or swap.take_asset != "CR"):
        raise ValueError("SELLER_SAGA_RESERVATION_INCONSISTENT")

    # AD7/AD8: correlación local y outbox participan del MISMO commit que HOLD.
    # El task token usa idpk para resolver esta ejecución incluso si AWS aún
    # no confirmó el ARN. El deadline no se inventa en cada reintento.
    if cycle.market_closes_at.tzinfo is None:
        raise ValueError("SELLER_MARKET_CLOSES_AT_TIMEZONE_REQUIRED")
    closes_at = cycle.market_closes_at.astimezone(timezone.utc)
    timeout_at = closes_at + timedelta(seconds=60)
    execution, created = get_or_create_saga_execution(
        session, idpk=bid.idpk, role="SELLER", cycle_id=bid.cycle_id,
        timeout_at=timeout_at, now=now,
    )
    if not created:
        # No duplicar la intención para una saga preexistente inconsistente.
        raise RuntimeError("SELLER_SAGA_EXECUTION_ALREADY_EXISTS")

    # Idpk + maquina identifican una sola ejecucion en AD7. No generar otro
    # msgId si se procesa el mismo bid ya reservado (lo evita el caller).
    execution_input = build_seller_execution_input(
        bid_idpk=bid.idpk, bid_msg_id=bid.msg_id,
        cycle_id=bid.cycle_id, offer_id=bid.offer_id,
        buyer_city_id=bid.counterpart_city_id, quantity_kwh=bid.quantity,
        give_amount=swap.give_amount, take_amount=swap.take_amount,
        wallet_idempotency_key=swap.idempotency_key,
        market_closes_at=closes_at,
    )
    # No fijar expires_at del OUTBOX a closesAt: si el relay se recupera luego
    # la saga aun debe iniciar y compensar la reserva, no quedar descartada.
    return enqueue_saga_start(
        session,
        idpk=bid.idpk, role="SELLER",
        state_machine_arn=arn,
        execution_input=execution_input,
        cycle_id=bid.cycle_id,
        now=now,
    )
