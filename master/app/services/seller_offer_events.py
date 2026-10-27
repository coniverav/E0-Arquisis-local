"""E2-029: cambios confirmados de la oferta seller y su difusión durable.

Las funciones NO hacen commit. El estado de negocio y el OutboundMessage
se guardan en la misma transacción del llamador (outbox).
"""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from uuid import uuid4

from sqlmodel import Session, select

from app.config import CITY_ID
from app.models import Cycle, MarketCycle, MarketOffer, OutboundMessage

CENT = Decimal("0.01")
MARKET_OFFER_TYPES = frozenset({"offer", "offer-updated", "offer-withdrawn"})


def _utc(now: datetime | None) -> datetime:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now debe incluir zona horaria")
    return now.astimezone(timezone.utc)


def _can_broadcast(session: Session, offer: MarketOffer, now: datetime) -> bool:
    """lastOfferAt es el límite oficial de los tres tipos de oferta v3."""
    if not offer.is_local or offer.city_id != CITY_ID:
        return False
    cycle = session.get(Cycle, offer.cycle_id)
    if cycle is None or cycle.market_last_offer_at is None:
        return False
    if now >= cycle.market_last_offer_at:
        return False
    if cycle.market_closes_at is not None and now >= cycle.market_closes_at:
        return False
    market = session.get(MarketCycle, offer.cycle_id)
    return market is None or market.closed_at is None


def _enqueue(session: Session, offer: MarketOffer, message_type: str, now: datetime) -> OutboundMessage:
    data = {"offerId": offer.offer_id}
    if message_type == "offer-updated":
        data.update({
            "remaining": offer.remaining_quantity,
            "pricePerEnergy": float(offer.price_per_energy),
        })
    msg_id, idpk = str(uuid4()), str(uuid4())
    cycle = session.get(Cycle, offer.cycle_id)
    payload = {
        "msgId": msg_id, "idpk": idpk, "type": message_type,
        "timestamp": now.isoformat().replace("+00:00", "Z"),
        "cycleId": offer.cycle_id, "cityId": CITY_ID, "data": data,
    }
    outbound = OutboundMessage(
        msg_id=msg_id, idpk=idpk, message_type=message_type,
        cycle_id=offer.cycle_id, payload=payload,
        exchange_name="market", routing_key=f"market.offer.{CITY_ID}",
        status="PENDING", attempt_count=0, dispatch_required=True,
        available_at=now, expires_at=cycle.market_last_offer_at,
        created_at=now,
    )
    session.add(outbound)
    session.flush()
    return outbound


def enqueue_confirmed_offer_update(
    session: Session, offer: MarketOffer, *, now: datetime | None = None
) -> bool:
    """Llamar SOLO después de cambiar remaining/precio en una transacción.

    Para ventas, esta función se llama únicamente tras swap-settled.
    Si la oferta se retiró, o venció lastOfferAt, no se emite un update tardío.
    """
    moment = _utc(now)
    if offer.status != "ACTIVE" or not _can_broadcast(session, offer, moment):
        return False
    _enqueue(session, offer, "offer-updated", moment)
    return True


def change_local_offer_price(
    session: Session, *, offer_id: str, price: Decimal, now: datetime | None = None
) -> bool:
    """Reajusta precio para bids FUTUROS. No modifica swaps ya propuestos."""
    moment = _utc(now)
    try:
        value = Decimal(str(price))
    except (ValueError, InvalidOperation) as exc:
        raise ValueError("pricePerEnergy inválido") from exc
    if not value.is_finite() or value <= 0:
        raise ValueError("pricePerEnergy debe ser positivo y finito")
    value = value.quantize(CENT, rounding=ROUND_HALF_UP)

    offer = session.exec(select(MarketOffer).where(
        MarketOffer.offer_id == offer_id, MarketOffer.is_local.is_(True),
        MarketOffer.city_id == CITY_ID,
    ).with_for_update()).first()
    if offer is None or offer.status != "ACTIVE":
        raise ValueError("Oferta local inexistente o no activa")
    if not _can_broadcast(session, offer, moment):
        raise ValueError("No se permite cambiar el precio después de lastOfferAt/cierre")
    if offer.price_per_energy == value:
        return False

    offer.price_per_energy = value
    offer.last_updated_at = moment
    session.add(offer)
    enqueue_confirmed_offer_update(session, offer, now=moment)
    return True


def withdraw_local_offer(
    session: Session, *, offer_id: str, now: datetime | None = None
) -> bool:
    """Retira la oferta para bids nuevos; no cancela reservas/swap en curso."""
    moment = _utc(now)
    offer = session.exec(select(MarketOffer).where(
        MarketOffer.offer_id == offer_id, MarketOffer.is_local.is_(True),
        MarketOffer.city_id == CITY_ID,
    ).with_for_update()).first()
    if offer is None:
        raise ValueError("Oferta local inexistente")
    if offer.status == "WITHDRAWN":
        return False  # idempotente: no duplica la difusión
    if offer.status != "ACTIVE":
        raise ValueError("Oferta ya cerrada")
    if not _can_broadcast(session, offer, moment):
        raise ValueError("No se permite retirar después de lastOfferAt/cierre")

    offer.status = "WITHDRAWN"
    offer.withdrawn_at = moment
    offer.last_updated_at = moment
    session.add(offer)
    _enqueue(session, offer, "offer-withdrawn", moment)
    return True


def close_local_offers(session: Session, *, cycle_id: str, now: datetime | None = None) -> int:
    """Marca cierre local al recibir market-closed o alcanzar closesAt.

    No publica offer-withdrawn: el cierre lo anuncia la central, y normalmente
    ya pasó lastOfferAt. Deshabilita difusiones pendientes que serían tardías.
    """
    moment = _utc(now)
    offers = session.exec(select(MarketOffer).where(
        MarketOffer.cycle_id == cycle_id, MarketOffer.is_local.is_(True),
        MarketOffer.city_id == CITY_ID, MarketOffer.status == "ACTIVE",
    ).with_for_update()).all()
    for offer in offers:
        offer.status = "CLOSED"
        offer.last_updated_at = moment
        session.add(offer)

    pending = session.exec(select(OutboundMessage).where(
        OutboundMessage.cycle_id == cycle_id,
        OutboundMessage.message_type.in_(tuple(MARKET_OFFER_TYPES)),
        OutboundMessage.status.in_(["PENDING", "FAILED"]),
        OutboundMessage.dispatch_required.is_(True),
    )).all()
    for item in pending:
        item.dispatch_required = False
        item.last_error = "MARKET_CLOSED"
        session.add(item)
    return len(offers)


def close_expired_local_offers(session: Session, *, now: datetime | None = None) -> int:
    """Respaldo cuando market-closed se retrasa o se pierde."""
    moment = _utc(now)
    cycles = session.exec(
        select(MarketOffer.cycle_id).join(
            Cycle, MarketOffer.cycle_id == Cycle.cycle_id
        ).where(
            MarketOffer.is_local.is_(True),
            MarketOffer.city_id == CITY_ID,
            MarketOffer.status == "ACTIVE",
            Cycle.market_closes_at.is_not(None),
            Cycle.market_closes_at <= moment,
        ).distinct()
    ).all()
    return sum(close_local_offers(session, cycle_id=cid, now=moment) for cid in cycles)
