"""E2-030: recepción y validación comercial de bids dirigidos a nuestra ciudad.

No inicia swaps, no reserva KWH y no hace commit; E2-031/AD4
serializa aceptación, inmoviliza energía y solicita la operación Wallet.
"""
from datetime import datetime, timezone
import re

from sqlmodel import Session, select

from ..config import CITY_ID
from ..models import (
    Cycle, MarketBid, MarketCycle, MarketOffer,
)
from .message_audit import INBOUND_NOT_PROCESSED, INBOUND_PROCESSED
from .seller_bid_rejections import (
    REJECTION_CODES, available_kwh, enqueue_bid_rejected,
)

def _utc(instant: datetime) -> datetime:
    if instant.tzinfo is None:
        raise ValueError("now requiere zona horaria")
    return instant.astimezone(timezone.utc)


def _send_rejection(
    session: Session, *, payload, buyer: str, reason: str, now: datetime,
    available: int | None = None,
) -> None:
    """Compatibilidad E2-030/E2-031: delega en el contrato E2-034."""
    enqueue_bid_rejected(
        session, payload=payload, buyer=buyer, reason=reason,
        now=now, available=available,
    )


def process_incoming_bid(session: Session, *, payload, now: datetime) -> tuple[str, str | None]:
    """Valida el bid y persiste resultado y posible rechazo sin hacer commit.

    Devuelve (estatus de auditoría, razón). Los rechazos de negocio son
    PROCESSED: el protocolo está bien formado y la respuesta es bid-rejected,
    no un NACK de transporte.
    """
    now = _utc(now)
    buyer = payload.cityId
    data = payload.data
    if (not payload.cycleId or not isinstance(buyer, str)
            or re.fullmatch(r"[A-Z0-9]{3}", buyer) is None
            or not isinstance(data, dict)
            or not isinstance(data.get("offerId"), str) or not data["offerId"]
            or type(data.get("quantity")) is not int or data["quantity"] <= 0):
        # Los errores sintácticos los NACKea el connector. Este chequeo protege
        # llamadas directas al backend y no crea estado inconsistente.
        return INBOUND_NOT_PROCESSED, "MALFORMED_BID"

    offer_id = data["offerId"]
    quantity = data["quantity"]
    key = str(payload.idpk)

    # La identidad de cityId fue comprobada antes por el connector (AMQP user_id).
    # Aquí sólo se maneja la regla comercial SELF_TRADE.
    cycle = session.get(Cycle, payload.cycleId)
    offer = session.exec(select(MarketOffer).where(
        MarketOffer.offer_id == offer_id,
        MarketOffer.cycle_id == payload.cycleId,
        MarketOffer.city_id == CITY_ID,
        MarketOffer.is_local.is_(True),
    ).with_for_update()).first()

    # Las filas INCOMING tienen FK a MarketOffer; si no existe, queda auditoría
    # durable del bid y una respuesta comercial, pero no un MarketBid huérfano.
    can_store_bid = offer is not None
    if buyer == CITY_ID:
        reason = "SELF_TRADE"
    elif (cycle is None or cycle.market_opens_at is None
          or cycle.market_last_bid_at is None
          or cycle.market_closes_at is None):
        reason = "MARKET_CLOSED"
    elif (now < _utc(cycle.market_opens_at)
          or now >= _utc(cycle.market_last_bid_at)
          or now >= _utc(cycle.market_closes_at)):
        reason = "MARKET_CLOSED"
    else:
        market = session.get(MarketCycle, payload.cycleId)
        if market is not None and market.closed_at is not None:
            reason = "MARKET_CLOSED"
        elif offer is None or offer.status != "ACTIVE":
            reason = "OFFER_WITHDRAWN"
        else:
            reason = None

    available = None
    if reason is None and offer is not None:
        # La aceptación posterior debe repetir esta comprobación dentro del
        # bloqueo y crear HOLD de forma atómica para cumplir AD4.
        available = available_kwh(session, offer)

        raw_market = (cycle.status_payload or {}).get("market", {})
        raw_min_bid = raw_market.get("minBid") if isinstance(raw_market, dict) else None
        if type(raw_min_bid) is not int or raw_min_bid < 1:
            # No adivinar el minBid del contrato oficial.
            reason = "MARKET_CLOSED"
        elif quantity < raw_min_bid:
            reason = "BID_TOO_SMALL"
        elif quantity > available:
            reason = "QUANTITY_UNAVAILABLE"

    # Es posible que un idpk de bid entrante coincida con un MarketBid ya
    # registrado como saliente: se debe rechazar la colisión, no sobrescribirlo.
    existing = session.exec(select(MarketBid).where(MarketBid.idpk == key)).first()
    if existing is not None:
        if (existing.direction == "INCOMING" and existing.cycle_id == payload.cycleId
                and existing.offer_id == offer_id
                and existing.counterpart_city_id == buyer
                and existing.quantity == quantity):
            # El endpoint reclama el idpk antes de llamar al servicio. Esta
            # rama es defensiva para invocaciones directas.
            return INBOUND_PROCESSED, "ALREADY_RECORDED"
        return INBOUND_NOT_PROCESSED, "BID_IDPK_CONFLICT"

    if can_store_bid:
        session.add(MarketBid(
            idpk=key, msg_id=str(payload.msgId), cycle_id=payload.cycleId,
            direction="INCOMING", offer_id=offer_id,
            counterpart_city_id=buyer, quantity=quantity,
            status="REJECTED" if reason else "PENDING",
            rejection_reason=reason,
            available_on_rejection=(available if reason == "QUANTITY_UNAVAILABLE" else None),
            created_at=now, responded_at=(now if reason else None),
        ))

    if reason:
        _send_rejection(
            session, payload=payload, buyer=buyer, reason=reason,
            now=now, available=(available if reason == "QUANTITY_UNAVAILABLE" else None),
        )
    session.flush()
    return INBOUND_PROCESSED, reason
