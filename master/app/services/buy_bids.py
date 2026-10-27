"""E2-038: crear bids de compra en outbox durable, sin publicar HTTP->AMQP.

La ciudad vendedora confirma recepción con ack y decide después si acepta.
Sólo los eventos de wallet pueden modificar el ledger de activos (AD5).
"""
from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlmodel import Session, select

from ..config import CITY_ID
from ..models import Cycle, MarketBid, MarketOffer, OutboundMessage
from .budget_carryover import lock_city_ledger
from .message_audit import OUTBOUND_FAILED, OUTBOUND_PENDING
from .offer_selection import get_buy_candidates


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def _bid_view(bid: MarketBid, *, duplicate: bool = False) -> dict:
    return {
        'cycleId': bid.cycle_id,
        'offerId': bid.offer_id,
        'quantity': bid.quantity,
        'idpk': bid.idpk,
        'msgId': bid.msg_id,
        'sellerCityId': bid.counterpart_city_id,
        'routingKey': f'city.{bid.counterpart_city_id}',
        'status': bid.status,
        'duplicate': duplicate,
    }


def enqueue_buy_bid(
    session: Session, *, cycle_id: str, offer_id: str, quantity: int,
    idpk: UUID, now: datetime | None = None,
) -> dict:
    """Una transacción persiste MarketBid + outbox; HTTP no publica al broker."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError('now requiere zona horaria')
    if type(quantity) is not int or quantity < 1:
        raise HTTPException(status_code=422, detail='quantity debe ser entero positivo')
    lock_city_ledger(session)
    key = str(idpk)
    existing = session.exec(select(MarketBid).where(MarketBid.idpk == key)).first()
    if existing is not None:
        if (existing.direction != 'OUTGOING' or existing.cycle_id != cycle_id
                or existing.offer_id != offer_id or existing.quantity != quantity):
            raise HTTPException(status_code=409, detail='idpk reutilizado para otro bid')
        return _bid_view(existing, duplicate=True)

    candidates = get_buy_candidates(session, cycle_id=cycle_id, now=now)
    if not candidates.canBid:
        raise HTTPException(status_code=409, detail=f'BID_WINDOW_CLOSED_OR_UNKNOWN: {candidates.reason}')
    selected = next((x for x in candidates.candidates if x.offerId == offer_id), None)
    if selected is None:
        raise HTTPException(status_code=409, detail='OFFER_NOT_ELIGIBLE')
    if (quantity < candidates.minBid or selected.remaining is None
            or quantity > selected.remaining):
        raise HTTPException(status_code=409, detail='QUANTITY_NOT_AVAILABLE_OR_TOO_SMALL')

    # Reservar conservadoramente las solicitudes locales aún no rechazadas;
    # esto NO constituye reserva ni confirmación en wallet/vendedor.
    outstanding = session.exec(select(MarketBid).where(
        MarketBid.offer_id == offer_id,
        MarketBid.cycle_id == cycle_id,
        MarketBid.direction == 'OUTGOING',
        MarketBid.status.in_(['PENDING', 'ACCEPTED']),
    )).all()
    if sum(bid.quantity for bid in outstanding) + quantity > selected.remaining:
        raise HTTPException(status_code=409, detail='LOCAL_PENDING_BIDS_EXCEED_REMAINING')

    # E2-018 asocia MarketBid a market_offers, mientras E2-017 almacena el
    # feed en market_feed_offers. Sólo crear la referencia si es necesaria;
    # el libro vigente SIEMPRE se obtiene desde el feed, nunca de esta copia.
    reference = session.exec(select(MarketOffer).where(MarketOffer.offer_id == offer_id)).first()
    if reference is None:
        reference = MarketOffer(
            offer_id=offer_id, cycle_id=cycle_id, city_id=selected.cityId,
            is_local=False, initial_quantity=selected.quantity or selected.remaining,
            remaining_quantity=selected.remaining, price_per_energy=selected.pricePerEnergy,
            status='ACTIVE', published_at=selected.lastEventAt,
            last_updated_at=selected.lastEventAt,
        )
        session.add(reference)
        session.flush()
    elif (reference.cycle_id != cycle_id or reference.city_id != selected.cityId):
        raise HTTPException(status_code=409, detail='OFFER_REFERENCE_CONFLICT')

    message_id = str(uuid4())
    while message_id == key:
        message_id = str(uuid4())
    data = {
        'idpk': key, 'msgId': message_id, 'type': 'bid',
        'timestamp': _iso(now), 'cityId': CITY_ID, 'cycleId': cycle_id,
        'data': {'offerId': offer_id, 'quantity': quantity},
    }
    bid = MarketBid(
        idpk=key, msg_id=message_id, cycle_id=cycle_id, direction='OUTGOING',
        offer_id=offer_id, counterpart_city_id=selected.cityId,
        quantity=quantity, status='PENDING', created_at=now,
    )
    session.add(bid)
    session.add(OutboundMessage(
        msg_id=message_id, idpk=key, message_type='bid', cycle_id=cycle_id,
        payload=data, routing_key=f'city.{selected.cityId}', exchange_name='energy.x',
        dispatch_required=True, status=OUTBOUND_PENDING, attempt_count=0,
        created_at=now, expires_at=candidates.lastBidAt,
    ))
    session.commit()
    session.refresh(bid)
    return _bid_view(bid)


def renew_failed_bid_dispatches(session: Session, *, now: datetime) -> int:
    """AD3: en reintentos se conserva idpk y se crea un msgId nuevo.

    Llamar bajo lock_city_ledger desde el dispatcher, antes de consultar outbox.
    Respuestas a cualquiera de los msgId se correlacionan por la outbox.
    """
    failed = session.exec(select(OutboundMessage).where(
        OutboundMessage.message_type == 'bid',
        OutboundMessage.status == OUTBOUND_FAILED,
        OutboundMessage.dispatch_required.is_(True),
    ).order_by(OutboundMessage.id)).all()
    count = 0
    for previous in failed:
        if previous.expires_at is not None and now >= previous.expires_at:
            previous.dispatch_required = False
            previous.last_error = 'LAST_BID_PASSED'
            session.add(previous)
            continue
        bid = session.exec(select(MarketBid).where(
            MarketBid.idpk == previous.idpk,
            MarketBid.direction == 'OUTGOING',
        )).first()
        previous.dispatch_required = False
        session.add(previous)
        if bid is None or bid.status != 'PENDING' or bid.msg_id != previous.msg_id:
            continue
        current = str(uuid4())
        while current == previous.idpk:
            current = str(uuid4())
        envelope = dict(previous.payload)
        envelope['msgId'] = current
        envelope['timestamp'] = _iso(now)
        session.add(OutboundMessage(
            msg_id=current, idpk=previous.idpk, message_type='bid',
            cycle_id=previous.cycle_id, payload=envelope,
            routing_key=previous.routing_key, exchange_name='energy.x',
            dispatch_required=True, status=OUTBOUND_PENDING,
            attempt_count=0, created_at=now, expires_at=previous.expires_at,
        ))
        bid.msg_id = current
        session.add(bid)
        count += 1
    session.flush()
    return count
