"""E3-044: persistir aceptación recibida + intención StartExecution en un commit.

No valida criptográficamente JWT (E3-045), no llama Wallet (E3-046) ni AWS.
El consumidor procesa bid-accepted sólo después de correlacionarlo con el
bid OUTGOING; entonces esta función congela el token localmente y crea una
única ejecución BUYER y su outbox. Flag apagado por defecto.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import os

from sqlmodel import Session, select

from ..models import Cycle, MarketBid, OutboundMessage, SwapRecord
from .buyer_saga_intent import build_buyer_execution_input
from .outbox_store import enqueue_saga_start
from .saga_tokens import get_or_create_saga_execution


def buyer_saga_enabled() -> bool:
    return os.getenv("E3_BUYER_SAGA_ENABLED", "false").strip().lower() in {
        "1", "true", "yes", "on"
    }


def _buyer_machine_arn() -> str:
    arn = os.getenv("E3_BUYER_STATE_MACHINE_ARN", "").strip()
    if not arn.startswith("arn:aws:states:"):
        raise ValueError("E3_BUYER_STATE_MACHINE_ARN_REQUIRED")
    return arn


def _expires_at(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("BUYER_SAGA_EXPIRY_REQUIRED")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("BUYER_SAGA_EXPIRY_INVALID") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("BUYER_SAGA_EXPIRY_TIMEZONE_REQUIRED")
    return parsed.astimezone(timezone.utc)


def enqueue_buyer_saga_on_acceptance(
    session: Session, *, bid: MarketBid, payload, now: datetime,
) -> OutboundMessage | None:
    """Se llama con el bid bloqueado, tras transición PENDING -> ACCEPTED.

    Si falta configuración o falla cualquier inserción, el llamador revierte
    también el bid, inbox y la intención; jamás queda doble coordinación E2/E3.
    """
    if not buyer_saga_enabled():
        return None
    arn = _buyer_machine_arn()
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("BUYER_SAGA_NOW_TIMEZONE_REQUIRED")
    now = now.astimezone(timezone.utc)
    data = payload.data
    acceptance_idpk = str(payload.idpk)
    if (payload.type != "bid-accepted" or bid.direction != "OUTGOING"
            or bid.status != "ACCEPTED" or bid.cycle_id != payload.cycleId
            or bid.swap_id != data.get("swapId")
            or bid.offer_id != data.get("offerId")
            or bid.counterpart_city_id != payload.cityId
            or bid.quantity != data.get("quantity")):
        raise ValueError("BUYER_SAGA_BID_IDENTITY_MISMATCH")
    token = data.get("swapToken")
    if not isinstance(token, str) or not token or len(token) > 16384:
        raise ValueError("BUYER_SAGA_TOKEN_INVALID")
    expires = _expires_at(data.get("expiresAt"))
    cycle = session.get(Cycle, bid.cycle_id)
    if cycle is None or cycle.market_closes_at is None:
        raise ValueError("BUYER_SAGA_CYCLE_CLOSE_REQUIRED")
    closes_at = cycle.market_closes_at
    if closes_at.tzinfo is None or closes_at.utcoffset() is None:
        raise ValueError("BUYER_SAGA_CLOSE_TIMEZONE_REQUIRED")

    # El consumidor E2 toma también bids ACCEPTED. No dejar un SwapRecord
    # alternativo si el worker antiguo llegó antes de activar el flag.
    old = session.exec(select(SwapRecord).where(
        SwapRecord.source_bid_idpk == bid.idpk,
    )).first()
    if old is not None:
        raise ValueError("BUYER_SAGA_ACCEPTANCE_ALREADY_OWNED")
    try:
        amount = Decimal(str(data.get("amount")))
    except (ValueError, InvalidOperation, TypeError) as exc:
        raise ValueError("BUYER_SAGA_AMOUNT_INVALID") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError("BUYER_SAGA_AMOUNT_INVALID")

    # Wallet no recibe ninguna llamada en E3-044. La llave de aceptación
    # tiene identidad estable desde el idpk oficial del bid-accepted.
    record = SwapRecord(
        idpk=acceptance_idpk,
        swap_id=bid.swap_id,
        cycle_id=bid.cycle_id,
        role="ACCEPTOR",
        counterpart_city_id=bid.counterpart_city_id,
        give_asset="CR", give_amount=amount,
        take_asset="KWH", take_amount=Decimal(bid.quantity),
        memo=None, status="SUBMITTED",
        idempotency_key=acceptance_idpk,
        accept_idempotency_key=acceptance_idpk,
        swap_token=token, expires_at=expires,
        source_bid_idpk=bid.idpk,
        created_at=now,
    )
    session.add(record)
    session.flush()
    execution_input = build_buyer_execution_input(
        acceptance_idpk=acceptance_idpk,
        bid_idpk=bid.idpk,
        bid_msg_id=str(data["target"]),
        cycle_id=bid.cycle_id,
        offer_id=bid.offer_id,
        seller_city_id=bid.counterpart_city_id,
        quantity_kwh=bid.quantity,
        amount_cr=amount,
        swap_id=bid.swap_id,
        accept_idempotency_key=record.accept_idempotency_key,
        expires_at=expires,
        closes_at=closes_at,
        swap_record_id=record.id,
        now=now,
    )
    timeout_at = datetime.fromisoformat(execution_input["timeoutAt"].replace("Z", "+00:00"))
    _, created = get_or_create_saga_execution(
        session, idpk=acceptance_idpk, role="BUYER",
        cycle_id=bid.cycle_id, swap_id=bid.swap_id,
        timeout_at=timeout_at, now=now,
    )
    if not created:
        raise RuntimeError("BUYER_SAGA_EXECUTION_ALREADY_EXISTS")
    # No fijar expires_at del outbox a expiresAt del JWT. El relay debe poder
    # iniciar una saga tardía para registrar un rechazo, no descartarla.
    return enqueue_saga_start(
        session, idpk=acceptance_idpk, role="BUYER",
        state_machine_arn=arn,
        execution_input=execution_input,
        cycle_id=bid.cycle_id, swap_id=bid.swap_id,
        now=now,
    )
