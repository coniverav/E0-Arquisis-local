"""E2-031 / AD4: reserva atómica y persistente por oferta.

Este módulo NO llama a Wallet ni hace commit. Así la transacción PostgreSQL
protege oferta + HOLD + intención de swap y termina antes de cualquier HTTP.

Los eventos swap-* de Wallet confirman o liberan HOLD (WalletService existente).
La publicación bid-accepted se debe efectuar solo tras swap-proposed, nunca
por el hecho de crear SwapRecord ni por recibir un HTTP 202.
"""
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from types import SimpleNamespace

from sqlmodel import Session, select

from ..config import CITY_ID
from ..models import Cycle, MarketBid, MarketCycle, MarketOffer, OfferReservation, SwapRecord
from .incoming_bids import _send_rejection

CENT = Decimal("0.01")


def _now_utc(now: datetime) -> datetime:
    if now.tzinfo is None:
        raise ValueError("now requiere zona horaria")
    return now.astimezone(timezone.utc)


def _available(session: Session, offer: MarketOffer) -> int:
    # IMPORTANTE: llamar únicamente tras SELECT FOR UPDATE de la oferta.
    # remaining_quantity ya descuenta COMMITTED; NO descontarlas otra vez.
    holds = session.exec(select(OfferReservation.quantity).where(
        OfferReservation.offer_id == offer.offer_id,
        OfferReservation.status == "HOLD",
    )).all()
    return max(0, offer.remaining_quantity - sum(holds))


def _reject(session: Session, bid: MarketBid, reason: str, now: datetime,
            available: int | None = None) -> dict:
    # La respuesta va al outbox en la misma transacción que el rechazo.
    _send_rejection(
        session,
        payload=SimpleNamespace(
            msgId=bid.msg_id,
            cycleId=bid.cycle_id,
            data={"offerId": bid.offer_id, "quantity": bid.quantity},
        ),
        buyer=bid.counterpart_city_id,
        reason=reason,
        now=now,
        available=available,
    )
    bid.status = "REJECTED"
    bid.rejection_reason = reason
    bid.available_on_rejection = available if reason == "QUANTITY_UNAVAILABLE" else None
    bid.responded_at = now
    session.add(bid)
    session.flush()
    return {"status": "REJECTED", "reason": reason, "available": available}


def reserve_incoming_bid(
    session: Session, *, bid_idpk: str, now: datetime | None = None,
) -> dict:
    """Serializa una aceptación usando la fila MarketOffer como mutex distribuido.

    Precondición: el bid fue validado y persistido como INCOMING/PENDING.
    No hace commit: permite integrar esta operación con inbox, idpk y auditoría.
    En casos concurrentes la segunda transacción espera el desbloqueo de la
    primera y vuelve a consultar las reservas ya confirmadas por PostgreSQL.
    """
    now = _now_utc(now or datetime.now(timezone.utc))

    # Consultar idpk para localizar la oferta; todavía no se decide nada.
    reference = session.exec(select(MarketBid).where(
        MarketBid.idpk == str(bid_idpk),
        MarketBid.direction == "INCOMING",
    )).first()
    if reference is None:
        raise ValueError("BID_UNKNOWN")

    # Siempre bloquear primero la oferta; es el orden de bloqueo AD4.
    offer = session.exec(select(MarketOffer).where(
        MarketOffer.offer_id == reference.offer_id,
        MarketOffer.cycle_id == reference.cycle_id,
        MarketOffer.city_id == CITY_ID,
        MarketOffer.is_local.is_(True),
    ).with_for_update()).first()

    # Proteger además el estado del propio bid ante consumidores concurrentes.
    bid = session.exec(select(MarketBid).where(
        MarketBid.idpk == str(bid_idpk),
        MarketBid.direction == "INCOMING",
    ).with_for_update()).first()
    if bid is None:
        raise ValueError("BID_UNKNOWN")

    if bid.status == "RESERVED":
        swap = session.exec(select(SwapRecord).where(
            SwapRecord.idpk == bid.idpk,
        )).first()
        if swap is None:
            raise RuntimeError("RESERVED_WITHOUT_SWAP_INTENT")
        return {"status": "RESERVED", "duplicate": True, "swapIdpk": swap.idpk,
                "idempotencyKey": swap.idempotency_key}
    if bid.status != "PENDING":
        return {"status": bid.status, "duplicate": True}

    cycle = session.get(Cycle, bid.cycle_id)
    market = session.get(MarketCycle, bid.cycle_id)
    if (cycle is None or cycle.market_opens_at is None
            or cycle.market_closes_at is None
            or now < _now_utc(cycle.market_opens_at)
            or now >= _now_utc(cycle.market_closes_at)
            or (market is not None and market.closed_at is not None)):
        return _reject(session, bid, "MARKET_CLOSED", now)

    if offer is None or offer.status != "ACTIVE":
        return _reject(session, bid, "OFFER_WITHDRAWN", now)

    if bid.counterpart_city_id == CITY_ID:
        return _reject(session, bid, "SELF_TRADE", now)

    market_data = (cycle.status_payload or {}).get("market", {})
    min_bid = market_data.get("minBid") if isinstance(market_data, dict) else None
    if type(min_bid) is not int or min_bid <= 0:
        return _reject(session, bid, "MARKET_CLOSED", now)
    if bid.quantity < min_bid:
        return _reject(session, bid, "BID_TOO_SMALL", now)

    available = _available(session, offer)
    if bid.quantity > available:
        return _reject(session, bid, "QUANTITY_UNAVAILABLE", now, available)

    existing_reservation = session.exec(select(OfferReservation).where(
        OfferReservation.bid_idpk == bid.idpk,
    )).first()
    existing_swap = session.exec(select(SwapRecord).where(
        SwapRecord.idpk == bid.idpk,
    )).first()
    if existing_reservation is not None or existing_swap is not None:
        # Nunca generar un segundo swap para el mismo bid ante estados inciertos.
        raise RuntimeError("INCONSISTENT_BID_RESERVATION")

    amount = (Decimal(bid.quantity) * offer.price_per_energy).quantize(
        CENT, rounding=ROUND_HALF_UP,
    )

    session.add(OfferReservation(
        offer_id=offer.offer_id,
        bid_idpk=bid.idpk,
        quantity=bid.quantity,
        status="HOLD",
        created_at=now,
    ))
    session.add(SwapRecord(
        # Correlación wallet v3 por data.idpk y eventos wallet_service.
        idpk=bid.idpk,
        idempotency_key=bid.idpk,
        cycle_id=bid.cycle_id,
        role="PROPOSER",
        counterpart_city_id=bid.counterpart_city_id,
        give_asset="KWH",
        give_amount=Decimal(bid.quantity),
        take_asset="CR",
        take_amount=amount,
        memo=offer.offer_id,
        status="SUBMITTED",  # intención durable; NO significa HTTP 202 ni swap-proposed
        created_at=now,
    ))
    bid.accepted_price_per_energy = offer.price_per_energy
    bid.status = "RESERVED"  # pendiente del evento wallet: NO es bid-accepted
    session.add(bid)
    session.flush()
    return {"status": "RESERVED", "duplicate": False,
            "swapIdpk": bid.idpk, "idempotencyKey": bid.idpk,
            "availableAfter": available - bid.quantity}
