"""E2-037: candidatas de compra ordenadas con la estrategia AD6 del ciclo.

No compra, no reserva, no publica en RabbitMQ y no escribe en PostgreSQL.
Los plazos y el minimo de bid son parametros oficiales del status-statement.
"""
from datetime import datetime, timedelta, timezone

from sqlmodel import Session

from ..config import CITY_ID
from ..models import Cycle
from ..schemas import MarketBuyCandidatesOut, MarketBookOfferOut
from .market_book import get_market_book
from .strategy_config import SELECTION_POLICIES, get_cycle_strategy


def _utc(value: datetime) -> datetime:
    """Normalizar para comparación; PostgreSQL conserva timestamptz."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _ordered_candidates(
    items: list[MarketBookOfferOut], policy: str
) -> list[MarketBookOfferOut]:
    # Desempate estable. No depender del orden del feed ni de llegada del broker.
    if policy == "CHEAPEST_EFFECTIVE_PRICE":
        return sorted(items, key=lambda item: (
            item.effectivePricePerEnergy, item.distanceMeters,
            -item.remaining, item.cityId, item.offerId,
        ))
    if policy == "MIN_DISTANCE":
        return sorted(items, key=lambda item: (
            item.distanceMeters, item.effectivePricePerEnergy,
            -item.remaining, item.cityId, item.offerId,
        ))
    return []


def get_buy_candidates(
    session: Session,
    *,
    cycle_id: str,
    now: datetime | None = None,
) -> MarketBuyCandidatesOut:
    """Calcula una vista read-only de candidatas disponibles para E2-038.

    No autoriza enviar bids: E2-038 deberá reevaluar el deadline y la
    disponibilidad de wallet justo antes de persistir el envío.
    """
    now = _utc(now or datetime.now(timezone.utc))
    cycle = session.get(Cycle, cycle_id)
    book = get_market_book(session, cycle_id=cycle_id, now=now)
    strategy = get_cycle_strategy(session, cycle_id) if cycle else None

    opens_at = _utc(cycle.market_opens_at) if cycle and cycle.market_opens_at else None
    last_bid = _utc(cycle.market_last_bid_at) if cycle and cycle.market_last_bid_at else None
    closes_at = _utc(cycle.market_closes_at) if cycle and cycle.market_closes_at else None
    span = strategy.span_seconds if strategy else None
    buying_opens_at = (opens_at + timedelta(seconds=span)
                       if opens_at is not None and span is not None and span >= 0
                       else None)

    raw_market = cycle.status_payload.get("market") if cycle and isinstance(cycle.status_payload, dict) else None
    raw_min_bid = raw_market.get("minBid") if isinstance(raw_market, dict) else None
    min_bid = raw_min_bid if type(raw_min_bid) is int and raw_min_bid >= 1 else None
    policy = strategy.offer_selection_policy if strategy else None

    reason: str | None = None
    if cycle is None or not cycle.status_idpk:
        reason = "MISSING_STATUS"
    elif strategy is None:
        reason = "MISSING_CYCLE_STRATEGY"
    elif min_bid is None:
        reason = "MISSING_MIN_BID"
    elif policy not in SELECTION_POLICIES or type(span) is not int or span < 0:
        reason = "INVALID_STRATEGY"
    elif opens_at is None or last_bid is None or closes_at is None or not (opens_at < last_bid <= closes_at):
        reason = "MISSING_OR_INVALID_WINDOW"
    elif book.marketClosed or now >= closes_at:
        reason = "MARKET_CLOSED"
    elif now >= last_bid:
        reason = "LAST_BID_PASSED"
    elif now < opens_at:
        reason = "MARKET_NOT_OPEN"
    elif now < buying_opens_at:
        reason = "WAITING_SPAN"

    items: list[MarketBookOfferOut] = []
    if reason is None:
        items = [offer for offer in book.offers if (
            offer.cityId != CITY_ID
            and offer.state == "ACTIVE"
            and not offer.stale
            and not offer.uncertain
            and offer.remaining is not None and offer.remaining >= min_bid
            and offer.distanceMeters is not None
            and offer.effectivePricePerEnergy is not None
            # Rutas disabled siguen siendo elegibles con tarifa punitiva oficial.
            and offer.routeEnabled is not None
        )]
        items = _ordered_candidates(items, policy)

    return MarketBuyCandidatesOut(
        cycleId=cycle_id,
        asOf=now,
        strategyVersion=strategy.version if strategy else None,
        selectionPolicy=policy,
        spanSeconds=span,
        minBid=min_bid,
        buyingOpensAt=buying_opens_at,
        lastBidAt=last_bid,
        closesAt=closes_at,
        marketClosed=book.marketClosed,
        canBid=(reason is None),
        reason=reason,
        total=len(items),
        candidates=items,
    )
