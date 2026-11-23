"""E3-048: diagnóstico AD7 de sagas y reservas posteriores al cierre.

Solo SELECT; no inventa liquidaciones, no muta Wallet ni libera HOLD.
Cero es el objetivo operativo, NO una afirmación sin medición en producción.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from ..models import Cycle, MarketBid, OfferReservation, SagaExecution, SwapRecord

ACTIVE_SAGA = frozenset({"PENDING_START", "RUNNING", "WAITING"})
TERMINAL_SWAP = frozenset({"SETTLED", "REJECTED", "EXPIRED", "CANCELLED"})


def recovery_health(session: Session, *, now: datetime | None = None,
                    sample_size: int = 20) -> dict:
    """Mide deudas AD7: abiertas a +60s, HOLD al cierre, swaps inciertos."""
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("RECOVERY_HEALTH_NOW_TZ_REQUIRED")
    moment = moment.astimezone(timezone.utc)
    if not 0 <= sample_size <= 100:
        raise ValueError("RECOVERY_HEALTH_SAMPLE_INVALID")
    overdue = session.exec(
        select(SagaExecution.idpk)
        .join(Cycle, Cycle.cycle_id == SagaExecution.cycle_id)
        .where(Cycle.market_closes_at.is_not(None),
               Cycle.market_closes_at <= moment - timedelta(seconds=60),
               SagaExecution.status.in_(ACTIVE_SAGA))
        .order_by(SagaExecution.idpk)
    ).all()
    held = session.exec(
        select(OfferReservation.bid_idpk)
        .join(MarketBid, MarketBid.idpk == OfferReservation.bid_idpk)
        .join(Cycle, Cycle.cycle_id == MarketBid.cycle_id)
        .where(Cycle.market_closes_at.is_not(None),
               Cycle.market_closes_at <= moment,
               OfferReservation.status == "HOLD")
        .order_by(OfferReservation.bid_idpk)
    ).all()
    uncertain = session.exec(
        select(SagaExecution.idpk)
        .join(SwapRecord, SwapRecord.idpk == SagaExecution.idpk)
        .where(SagaExecution.status.in_({"TIMED_OUT", "FAILED"}),
               ~SwapRecord.status.in_(TERMINAL_SWAP))
        .order_by(SagaExecution.idpk)
    ).all()
    return {
        "measuredAt": moment.isoformat(),
        "overdueExecutionsAfter60s": len(overdue),
        "heldReservationsAfterClose": len(held),
        "unresolvedTimedOutOrFailedSwaps": len(uncertain),
        "examples": {
            "overdueExecutionIdpks": list(overdue[:sample_size]),
            "heldBidIdpks": list(held[:sample_size]),
            "unresolvedSagaIdpks": list(uncertain[:sample_size]),
        },
    }
