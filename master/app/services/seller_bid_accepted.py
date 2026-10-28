"""E2-033: notificar bid-accepted únicamente tras swap-proposed de Wallet.

El connector autentica el origen central vía AMQP. Este servicio **no** hace
verificación criptográfica del JWT (responsabilidad del COMPRADOR según v3.0.1);
comprueba formato ES256 y coincidencia de claims, evento y estado persistido.
Sin evento central o con datos faltantes/inconsistentes no publica nada.

Llamar dentro de la MISMA transacción de process_swap_event y la auditoría inbox.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from uuid import uuid4

import jwt
from sqlmodel import Session, select

from ..config import CITY_ID
from ..models import MarketBid, OfferReservation, OutboundMessage, SwapRecord


def _amount(value) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    try:
        amount = Decimal(str(value))
        return amount if amount.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def _date(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


def _legs_match(give: object, take: object, record: SwapRecord) -> bool:
    return (
        isinstance(give, dict)
        and isinstance(take, dict)
        and give.get('asset') == record.give_asset
        and take.get('asset') == record.take_asset
        and _amount(give.get('amount')) == record.give_amount
        and _amount(take.get('amount')) == record.take_amount
    )


def _matching_wallet_token(
    token: object, record: SwapRecord, *, swap_id: str,
    expires_at: datetime, now: datetime,
) -> bool:
    """Comprueba coherencia de claims del JWT de evento central.

    La firma ES256 se verifica en E2 del lado COMPRADOR con JWKS. NO interpretar
    decode sin firma como autenticación criptográfica ni usarlo con eventos
    provenientes de fuentes no confiables.
    """
    if not isinstance(token, str) or not token or len(token) > 16384:
        return False
    try:
        header = jwt.get_unverified_header(token)
        if header.get('alg') != 'ES256':
            return False
        claims = jwt.decode(token, options={'verify_signature': False,
                                            'verify_exp': False, 'verify_aud': False})
    except (jwt.InvalidTokenError, ValueError, TypeError):
        return False
    exp = claims.get('exp')
    iat = claims.get('iat')
    if type(exp) is not int or type(iat) is not int:
        return False
    if exp <= int(now.timestamp()) or iat > int(now.timestamp()) + 5:
        return False
    if abs(exp - expires_at.timestamp()) > 1.5:
        return False
    return (
        claims.get('iss') == 'wallet.energyshark'
        and claims.get('sub') == swap_id
        and claims.get('proposer') == CITY_ID
        and claims.get('memo') == record.memo
        and _legs_match(claims.get('give'), claims.get('take'), record)
    )


def enqueue_bid_accepted_after_swap_proposed(
    session: Session, *, record: SwapRecord, event_data: dict,
    sender: str | None, now: datetime,
) -> bool:
    """Inserta a lo sumo un outbox para el bid; no hace commit.

    True indica encolado, False indica evento no elegible o ya notificado.
    La clave económica (precio congelado) se fija al reservar E2-031.
    """
    if now.tzinfo is None:
        raise ValueError('now debe incluir zona horaria')
    now = now.astimezone(timezone.utc)
    if sender != 'central' or record.role != 'PROPOSER' or record.status != 'PROPOSED':
        return False
    if record.give_asset != 'KWH' or record.take_asset != 'CR':
        return False
    if not isinstance(event_data, dict) or event_data.get('idpk') != record.idempotency_key:
        return False
    swap_id = event_data.get('swapId')
    token = event_data.get('token')  # Wallet E2 v3.0.1: 'token', no 'swapToken'.
    expires_at = _date(event_data.get('expiresAt'))
    if not isinstance(swap_id, str) or not swap_id or not expires_at or expires_at <= now:
        return False
    if record.swap_id != swap_id or record.memo != event_data.get('memo'):
        return False
    if event_data.get('proposer') != CITY_ID:
        return False
    if not _legs_match(event_data.get('give'), event_data.get('take'), record):
        return False
    if not _matching_wallet_token(token, record, swap_id=swap_id,
                                  expires_at=expires_at, now=now):
        return False
    if record.swap_token != token or record.expires_at is None:
        return False
    if abs((record.expires_at.astimezone(timezone.utc) - expires_at).total_seconds()) > 1.5:
        return False

    # Un mismo bid solo puede adquirir la respuesta una vez; con distintos
    # envelopes swap-proposed el bloqueo de esta fila serializa en PostgreSQL.
    bid = session.exec(select(MarketBid).where(
        MarketBid.idpk == record.idpk,
        MarketBid.direction == 'INCOMING',
    ).with_for_update()).first()
    if bid is None or bid.status != 'RESERVED':
        return False
    if (bid.cycle_id != record.cycle_id or bid.offer_id != record.memo
            or bid.counterpart_city_id != record.counterpart_city_id
            or record.give_amount != Decimal(bid.quantity)):
        return False
    if bid.accepted_price_per_energy is None or bid.accepted_price_per_energy < 0:
        # No usar el precio actual de la oferta: pudo variar tras el bid.
        return False
    if (bid.accepted_price_per_energy * Decimal(bid.quantity)).quantize(
            Decimal('0.01'), rounding=ROUND_HALF_UP) != record.take_amount:
        return False
    reservation = session.exec(select(OfferReservation).where(
        OfferReservation.bid_idpk == bid.idpk,
        OfferReservation.offer_id == bid.offer_id,
        OfferReservation.status == 'HOLD',
    )).first()
    if reservation is None or reservation.quantity != bid.quantity:
        return False
    if session.exec(select(OutboundMessage).where(
        OutboundMessage.message_type == 'bid-accepted',
        OutboundMessage.target_msg_id == bid.msg_id,
    )).first() is not None:
        return False

    message_id = str(uuid4())
    response_idpk = str(uuid4())
    payload = {
        'idpk': response_idpk, 'msgId': message_id,
        'type': 'bid-accepted', 'timestamp': now.isoformat().replace('+00:00', 'Z'),
        'cityId': CITY_ID, 'cycleId': bid.cycle_id,
        'data': {
            'target': bid.msg_id, 'offerId': bid.offer_id,
            'quantity': bid.quantity,
            'pricePerEnergy': float(bid.accepted_price_per_energy),
            'amount': float(record.take_amount),
            'swapId': swap_id, 'swapToken': token,
            'expiresAt': expires_at.isoformat().replace('+00:00', 'Z'),
        },
    }
    session.add(OutboundMessage(
        msg_id=message_id, idpk=response_idpk,
        message_type='bid-accepted', cycle_id=bid.cycle_id,
        target_msg_id=bid.msg_id,
        payload=payload, routing_key=f'city.{bid.counterpart_city_id}',
        exchange_name='energy.x', status='PENDING',
        attempt_count=0, dispatch_required=True, created_at=now,
        # Evitar publicar un token vencido al recuperar una outbox retrasada.
        expires_at=expires_at,
    ))
    bid.status = 'ACCEPTED'  # aceptación comunicada DURABLEMENTE; aún no settled
    bid.swap_id = swap_id
    bid.responded_at = now
    session.add(bid)
    session.flush()
    return True
