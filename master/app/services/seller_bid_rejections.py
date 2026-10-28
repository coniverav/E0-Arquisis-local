"""E2-034 / RF03: un único contrato para rechazos comerciales de bids.

Solo se llama DESPUÉS de validar la identidad AMQP en el connector. El cálculo
available_kwh requiere que el llamador tenga bloqueada la fila MarketOffer con
SELECT FOR UPDATE, para coordinar master, master2 y worker (AD4).

Todas las escrituras usan la Session del llamador: el inbox, MarketBid y outbox
se confirman o revierten juntos; aquí no hacemos commit ni publicamos RabbitMQ.
"""
from datetime import datetime, timezone
import re
from uuid import UUID, uuid4

from sqlmodel import Session, select

from ..config import CITY_ID
from ..models import MarketOffer, OfferReservation, OutboundMessage

REJECTION_CODES: dict[str, int] = {
    "QUANTITY_UNAVAILABLE": 409,
    "BID_TOO_SMALL": 422,
    "OFFER_WITHDRAWN": 410,
    "MARKET_CLOSED": 410,
    "SELF_TRADE": 422,
}


def available_kwh(session: Session, offer: MarketOffer) -> int:
    """Disponibilidad real de la oferta bloqueada: remaining - HOLD.

    remaining_quantity YA descuenta ventas COMMITTED, por lo tanto no se
    deben restar nuevamente. Las reservas RELEASED tampoco consumen capacidad.
    """
    holds = session.exec(select(OfferReservation.quantity).where(
        OfferReservation.offer_id == offer.offer_id,
        OfferReservation.status == "HOLD",
    )).all()
    return max(0, offer.remaining_quantity - sum(holds))


def enqueue_bid_rejected(
    session: Session, *, payload, buyer: str, reason: str, now: datetime,
    available: int | None = None,
) -> OutboundMessage:
    """Crea bid-rejected conforme al protocolo v3 y AD4, sin commit.

    available es OBLIGATORIO y exacto para QUANTITY_UNAVAILABLE; no se inventa
    cero cuando falta. Para los demás motivos se omite por completo.
    """
    if reason not in REJECTION_CODES:
        raise ValueError(f"Motivo bid-rejected no soportado: {reason}")
    if not isinstance(buyer, str) or re.fullmatch(r"[A-Z0-9]{3}", buyer) is None:
        raise ValueError("buyer inválido")
    if now.tzinfo is None:
        raise ValueError("now requiere zona horaria")
    if not isinstance(payload.cycleId, str) or not payload.cycleId:
        raise ValueError("cycleId requerido")
    if not isinstance(payload.data, dict):
        raise ValueError("data inválido")
    offer_id = payload.data.get("offerId")
    requested = payload.data.get("quantity")
    if not isinstance(offer_id, str) or not offer_id:
        raise ValueError("offerId requerido")
    if type(requested) is not int or requested < 1:
        raise ValueError("quantity inválida")
    try:
        target = str(UUID(str(payload.msgId)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("target msgId no es UUID") from exc

    if reason == "QUANTITY_UNAVAILABLE":
        if type(available) is not int or available < 0:
            raise ValueError("QUANTITY_UNAVAILABLE requiere available entero >= 0")
        if requested <= available:
            raise ValueError("QUANTITY_UNAVAILABLE requiere requested > available")
    elif available is not None:
        raise ValueError(f"available solo corresponde a QUANTITY_UNAVAILABLE: {reason}")

    # Guardia defensiva para llamadas repetidas dentro del mismo camino de
    # negocio. La unicidad fuerte frente a reenvíos AMQP la garantiza
    # ProcessedIdpk en el ingress; la reserva/estado del bid también se bloquean.
    previous = session.exec(select(OutboundMessage).where(
        OutboundMessage.message_type == "bid-rejected",
        OutboundMessage.target_msg_id == target,
    ).order_by(OutboundMessage.id)).first()
    if previous is not None:
        old = previous.payload or {}
        expected_data = {"target": target, "offerId": offer_id, "requested": requested}
        if available is not None:
            expected_data["available"] = available
        if (old.get("reason") != reason or old.get("code") != REJECTION_CODES[reason]
                or old.get("data") != expected_data
                or previous.routing_key != f"city.{buyer}"
                or previous.cycle_id != payload.cycleId):
            raise RuntimeError("CONFLICTING_BID_REJECTION")
        return previous

    message_id = str(uuid4())
    response_idpk = str(uuid4())
    while response_idpk == message_id:
        response_idpk = str(uuid4())
    now = now.astimezone(timezone.utc)
    data = {"target": target, "offerId": offer_id, "requested": requested}
    if reason == "QUANTITY_UNAVAILABLE":
        data["available"] = available
    envelope = {
        "msgId": message_id, "idpk": response_idpk,
        "type": "bid-rejected", "timestamp": now.isoformat().replace("+00:00", "Z"),
        "cityId": CITY_ID, "cycleId": payload.cycleId,
        "reason": reason, "code": REJECTION_CODES[reason], "data": data,
    }
    outbound = OutboundMessage(
        msg_id=message_id, idpk=response_idpk, message_type="bid-rejected",
        cycle_id=payload.cycleId, target_msg_id=target,
        payload=envelope, routing_key=f"city.{buyer}", exchange_name="energy.x",
        dispatch_required=True, status="PENDING", attempt_count=0,
        created_at=now,
    )
    session.add(outbound)
    session.flush()
    return outbound
