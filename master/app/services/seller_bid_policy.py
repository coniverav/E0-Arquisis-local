"""E2-063 / RF03 / AD6: política de aceptación por oferta del ciclo.

AD6: la política se consulta SOLO desde Cycle.strategy_version (snapshot),
no desde la versión global ACTIVE/PENDING. No usa memoria mutable ni cache.

FIFO: aceptar al ingresar (E2-031).
MAX_REMAINING: acumular una ventana breve y procesar primero bids grandes.
BATCH: acumular una ventana breve y priorizar bids pequeños para aumentar
       la cantidad de compradores atendidos.

En todos los casos la reserva real ocurre EXCLUSIVAMENTE en
seller_bid_acceptance.reserve_incoming_bid, con SELECT FOR UPDATE de MarketOffer.
No hay HTTP ni commit interno; el scheduler usa una transacción breve.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging

from sqlalchemy import func
from sqlmodel import Session, select

from ..config import CITY_ID
from ..models import Cycle, MarketBid, MarketOffer, StrategyConfig
from .seller_bid_acceptance import reserve_incoming_bid

logger = logging.getLogger(__name__)

FIFO = "FIFO"
MAX_REMAINING = "MAX_REMAINING"
BATCH = "BATCH"
SUPPORTED = frozenset({FIFO, MAX_REMAINING, BATCH})
# Ventana de observación corta; independiente de span de compra (RF06).
COLLECT_SECONDS = 2
# Evita esperar al límite exacto lastBidAt para iniciar la reserva.
CUTOFF_GUARD_SECONDS = 5


def cycle_acceptance_policy(session: Session, cycle_id: str) -> str:
    """Política congelada de AD6, sin consultar la configuración pendiente.

    Compatibilidad: los ciclos anteriores a E2-026, sin strategy_version,
    conservan FIFO. No se crea ni activa ninguna versión desde el ingreso.
    """
    cycle = session.get(Cycle, cycle_id)
    if cycle is None:
        raise ValueError("BID_CYCLE_UNKNOWN")
    if cycle.strategy_version is None:
        return FIFO
    strategy = session.exec(select(StrategyConfig).where(
        StrategyConfig.version == cycle.strategy_version
    )).one_or_none()
    if strategy is None or strategy.bid_acceptance_policy not in SUPPORTED:
        raise ValueError("INVALID_CYCLE_ACCEPTANCE_STRATEGY")
    return strategy.bid_acceptance_policy


def should_reserve_on_ingress(session: Session, cycle_id: str) -> bool:
    """Solo FIFO toma la decisión inmediatamente al recibir el bid."""
    return cycle_acceptance_policy(session, cycle_id) == FIFO


def _utc(now: datetime | None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        raise ValueError("now debe incluir timezone")
    return value.astimezone(timezone.utc)


def _pending(session: Session, offer_id: str) -> list[MarketBid]:
    return list(session.exec(select(MarketBid).where(
        MarketBid.offer_id == offer_id,
        MarketBid.direction == "INCOMING",
        MarketBid.status == "PENDING",
    ).order_by(MarketBid.created_at, MarketBid.id)).all())


def _ordered(bids: list[MarketBid], policy: str) -> list[MarketBid]:
    # Orden secundario estable para dos bids con el mismo tamaño o tiempo.
    if policy == MAX_REMAINING:
        return sorted(bids, key=lambda b: (-b.quantity, b.created_at, b.idpk))
    if policy == BATCH:
        return sorted(bids, key=lambda b: (b.quantity, b.created_at, b.idpk))
    return sorted(bids, key=lambda b: (b.created_at, b.idpk))


def process_due_seller_bids(
    session: Session,
    *,
    now: datetime | None = None,
    max_offers: int = 25,
) -> dict[str, int]:
    """Decide lotes pendientes; reserva usando exclusivamente AD4.

    El lock de cada MarketOffer se toma antes de seleccionar y ordenar bids.
    Si otro worker mantiene esa oferta, SKIP LOCKED la deja para otro tick.
    Si un ciclo ya venció, reserve_incoming_bid rechaza MARKET_CLOSED; no se
    reservan KWH fuera de ventana. El outbox y el resultado son transaccionales.

    El llamador debe hacer commit/rollback. Nunca hace llamadas HTTP.
    """
    moment = _utc(now)
    if type(max_offers) is not int or max_offers < 1:
        raise ValueError("max_offers debe ser entero positivo")

    # No bloquear ofertas sin bids pendientes. El query es solo candidato:
    # bajo FOR UPDATE se vuelve a leer el conjunto actualizado.
    candidate_ids = session.exec(
        select(MarketOffer.offer_id)
        .join(MarketBid, MarketBid.offer_id == MarketOffer.offer_id)
        .where(
            MarketOffer.is_local.is_(True),
            MarketOffer.city_id == CITY_ID,
            MarketBid.direction == "INCOMING",
            MarketBid.status == "PENDING",
        )
        .group_by(MarketOffer.offer_id, MarketOffer.id)
        .order_by(func.min(MarketBid.created_at), MarketOffer.id)
        .limit(max_offers)
    ).all()

    result = {"offers_processed": 0, "reserved": 0, "rejected": 0,
              "waiting": 0, "skipped_fifo": 0}
    for offer_id in candidate_ids:
        # AD4: compartido con el ingress, retire, repricing y settlement.
        offer = session.exec(select(MarketOffer).where(
            MarketOffer.offer_id == offer_id,
            MarketOffer.is_local.is_(True),
            MarketOffer.city_id == CITY_ID,
        ).with_for_update(skip_locked=True)).first()
        if offer is None:
            continue
        bids = _pending(session, offer.offer_id)
        if not bids:
            continue
        policy = cycle_acceptance_policy(session, offer.cycle_id)
        if policy == FIFO:
            # Compatibilidad con bids FIFO pendientes por fallas previas.
            result["skipped_fifo"] += 1
            order = _ordered(bids, FIFO)
        else:
            cycle = session.get(Cycle, offer.cycle_id)
            if cycle is None or cycle.market_last_bid_at is None:
                due = True  # reserve_incoming_bid rechazará MARKET_CLOSED.
            else:
                last_bid_at = cycle.market_last_bid_at
                if last_bid_at.tzinfo is None:
                    last_bid_at = last_bid_at.replace(tzinfo=timezone.utc)
                # Si queda menos de 5s, procesar sin esperar todo el lote.
                due = (moment >= _utc(bids[0].created_at)
                       + timedelta(seconds=COLLECT_SECONDS)
                       or moment >= last_bid_at - timedelta(seconds=CUTOFF_GUARD_SECONDS))
                if not due:
                    result["waiting"] += len(bids)
                    continue
            order = _ordered(bids, policy)

        result["offers_processed"] += 1
        for bid in order:
            # Debe bloquear oferta y bid, validar ventana y cantidad de nuevo.
            # reserve_incoming_bid es idempotente ante ejecución concurrente.
            outcome = reserve_incoming_bid(session, bid_idpk=bid.idpk, now=moment)
            if outcome["status"] == "RESERVED" and not outcome.get("duplicate"):
                result["reserved"] += 1
            elif outcome["status"] == "REJECTED" and not outcome.get("duplicate"):
                result["rejected"] += 1
    return result
