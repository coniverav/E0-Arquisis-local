"""E2-039: correlación de respuestas comerciales de bids salientes.

ACK confirma recepción de protocolo; no es aceptación del bid ni pago en wallet.
No aplica asientos contables ni acepta swaps: eso corresponde al flujo wallet.
"""
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from uuid import UUID

from sqlmodel import Session, select

from ..models import MarketBid, OutboundMessage
from ..services.message_audit import INBOUND_NOT_PROCESSED, INBOUND_PROCESSED

REJECTIONS = {
    'QUANTITY_UNAVAILABLE': 409,
    'OFFER_WITHDRAWN': 410,
    'BID_TOO_SMALL': 422,
    'MARKET_CLOSED': 410,
    'SELF_TRADE': 422,
}


def _positive_int(value):
    return type(value) is int and value > 0


def _price(value) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError('pricePerEnergy y amount deben ser numéricos')
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError('Precio inválido') from exc
    if not result.is_finite() or result < 0:
        raise ValueError('Precio inválido')
    return result


def _uuid(value) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError('data.target debe ser UUID') from exc


def process_bid_reply(session: Session, *, payload, now: datetime) -> tuple[str, str | None]:
    """Actualiza MarketBid sólo cuando sender/target/offer/cycle coinciden.

    Devuelve estado de auditoría y razón; participa en la transacción
    del inbox y ProcessedIdpk (el llamador realiza commit o rollback).
    """
    if payload.type not in {'bid-accepted', 'bid-rejected'}:
        raise ValueError('tipo de respuesta de bid no soportado')
    data = payload.data
    target = _uuid(data.get('target'))
    outbound = session.exec(select(OutboundMessage).where(
        OutboundMessage.msg_id == target,
        OutboundMessage.message_type == 'bid',
    )).first()
    if outbound is None:
        return INBOUND_NOT_PROCESSED, 'UNKNOWN_BID_TARGET'

    bid = session.exec(select(MarketBid).where(
        MarketBid.idpk == outbound.idpk,
        MarketBid.direction == 'OUTGOING',
    ).with_for_update()).first()
    if bid is None:
        return INBOUND_NOT_PROCESSED, 'UNKNOWN_OUTGOING_BID'
    if (payload.cycleId != bid.cycle_id or outbound.cycle_id != bid.cycle_id
            or payload.cityId != bid.counterpart_city_id
            or data.get('offerId') != bid.offer_id):
        return INBOUND_NOT_PROCESSED, 'BID_CORRELATION_MISMATCH'

    # Una respuesta terminal nunca se pisa con otro resultado/reenvío.
    if bid.status != 'PENDING':
        return INBOUND_NOT_PROCESSED, 'BID_ALREADY_RESOLVED'

    if payload.type == 'bid-accepted':
        if not _positive_int(data.get('quantity')) or data['quantity'] != bid.quantity:
            return INBOUND_NOT_PROCESSED, 'BID_QUANTITY_MISMATCH'
        if (not isinstance(data.get('swapId'), str) or not data['swapId']
                or not isinstance(data.get('swapToken'), str) or not data['swapToken']
                or not isinstance(data.get('expiresAt'), str)):
            return INBOUND_NOT_PROCESSED, 'INCOMPLETE_ACCEPTANCE'
        try:
            expires_at = datetime.fromisoformat(data['expiresAt'].replace('Z', '+00:00'))
        except ValueError:
            return INBOUND_NOT_PROCESSED, 'INVALID_SWAP_EXPIRY'
        if expires_at.tzinfo is None:
            return INBOUND_NOT_PROCESSED, 'INVALID_SWAP_EXPIRY'
        try:
            price = _price(data.get('pricePerEnergy'))
            amount = _price(data.get('amount'))
        except ValueError:
            return INBOUND_NOT_PROCESSED, 'INVALID_ACCEPTANCE_PRICE'
        expected = (price * bid.quantity).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        if amount != expected:
            return INBOUND_NOT_PROCESSED, 'ACCEPTANCE_AMOUNT_MISMATCH'
        bid.swap_id = data['swapId']
        bid.status = 'ACCEPTED'
        bid.responded_at = now
    else:
        reason = getattr(payload, 'reason', None)
        code = getattr(payload, 'code', None)
        if REJECTIONS.get(reason) != code:
            return INBOUND_NOT_PROCESSED, 'INVALID_BID_REJECTION_REASON'
        requested = data.get('requested')
        if requested is not None and requested != bid.quantity:
            return INBOUND_NOT_PROCESSED, 'BID_QUANTITY_MISMATCH'
        if reason == 'QUANTITY_UNAVAILABLE':
            available = data.get('available')
            if type(available) is not int or available < 0:
                return INBOUND_NOT_PROCESSED, 'INVALID_AVAILABLE'
            bid.available_on_rejection = available
        elif 'available' in data:
            return INBOUND_NOT_PROCESSED, 'AVAILABLE_NOT_APPLICABLE'
        bid.rejection_reason = reason
        bid.status = 'REJECTED'
        bid.responded_at = now

    session.add(bid)
    # Una respuesta terminal cancela reintentos aún no publicados: no se vuelve
    # a ofertar la misma compra después de conocer la decisión del vendedor.
    for attempt in session.exec(select(OutboundMessage).where(
        OutboundMessage.idpk == bid.idpk,
        OutboundMessage.message_type == 'bid',
        OutboundMessage.dispatch_required.is_(True),
    )).all():
        attempt.dispatch_required = False
        session.add(attempt)
    # No ledger ni wallet; la aceptación de bid sólo indica propuesta de swap.
    return INBOUND_PROCESSED, None
