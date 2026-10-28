"""E2-041: aceptación asíncrona e idempotente de swaps (comprador).

Flujo:

1. El vendedor responde `bid-accepted` con `swapId`, `swapToken` y
   `expiresAt`. El ingress lo audita y `bid_responses.py` marca el bid como
   `ACCEPTED`; no se llama a la wallet en esa transacción.
2. Este worker toma esos bids aceptados que todavía no tienen `SwapRecord`,
   verifica el token (E2-040) y crea un `SwapRecord` con rol `ACCEPTOR` y una
   `accept_idempotency_key` durable.
3. Llama `POST /swaps/{id}/accept`. Un HTTP 202 sólo confirma recepción: el
   swap queda `ACCEPT_SUBMITTED` hasta que llegue `swap-settled`.
4. Timeout, 429 o 5xx dejan el intento persistido para reintentar con la
   misma llave. Un 4xx es terminal y queda como `REJECTED` con el motivo.

Un token inválido nunca llega a la wallet: se registra un `SwapRecord`
`REJECTED` que fija el vínculo con el bid y evita reintentos infinitos.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import logging
from typing import Any, Optional
import uuid

from sqlalchemy import and_, exists, or_
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.models import (
    InboundMessage,
    MarketBid,
    OutboundMessage,
    SwapRecord,
)
from app.services.message_audit import INBOUND_PROCESSED
from app.services.swap_tokens import SwapTokenError, verify_swap_token
from app.services.wallet_client import (
    WalletError,
    WalletRateLimitError,
    WalletTimeoutError,
)
from app.services.wallet_service import WalletService

logger = logging.getLogger(__name__)

RETRYABLE_WALLET_STATUSES = {408, 425, 429, 500, 502, 503, 504}
RETRYABLE_TOKEN_REASONS = {
    "SWAP_TOKEN_JWKS_UNAVAILABLE",
    "SWAP_TOKEN_JWKS_NOT_CONFIGURED",
}


def _decimal(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def _parse_instant(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _accepted_bid_candidates(session: Session, limit: int) -> list[MarketBid]:
    """Bids aceptados que aún no tienen un SwapRecord de aceptación."""

    linked = exists(
        select(SwapRecord.id).where(SwapRecord.source_bid_idpk == MarketBid.idpk)
    )
    return list(session.exec(
        select(MarketBid)
        .where(
            MarketBid.direction == "OUTGOING",
            MarketBid.status == "ACCEPTED",
            MarketBid.swap_id.is_not(None),
            ~linked,
        )
        .order_by(MarketBid.id)
        .limit(limit)
    ).all())


def _accept_work(session: Session, limit: int) -> list[SwapRecord]:
    """Aceptaciones que deben (re)intentarse.

    Un `ACCEPT_SUBMITTED` sin error ya recibió su 202 y espera el evento
    final: no se reenvía. Un `SUBMITTED` es una intención durable que aún no
    llegó a la wallet; un `ACCEPT_SUBMITTED` con error reintentable se
    recupera con la misma llave.
    """

    return list(session.exec(
        select(SwapRecord)
        .where(
            SwapRecord.role == "ACCEPTOR",
            SwapRecord.source_bid_idpk.is_not(None),
            SwapRecord.accept_idempotency_key.is_not(None),
            or_(
                SwapRecord.status == "SUBMITTED",
                and_(
                    SwapRecord.status == "ACCEPT_SUBMITTED",
                    SwapRecord.accept_error.is_not(None),
                ),
            ),
        )
        .order_by(SwapRecord.id)
        .limit(limit)
    ).all())


def _bid_accept_payload(session: Session, bid: MarketBid) -> Optional[InboundMessage]:
    """Busca el `bid-accepted` procesado que apunta a algún intento del bid."""

    targets = list(session.exec(
        select(OutboundMessage.msg_id).where(
            OutboundMessage.idpk == bid.idpk,
            OutboundMessage.message_type == "bid",
        )
    ).all())
    if not targets:
        return None

    target_expr = InboundMessage.payload["data"]["target"].astext
    return session.exec(
        select(InboundMessage)
        .where(
            InboundMessage.message_type == "bid-accepted",
            InboundMessage.status == INBOUND_PROCESSED,
            target_expr.in_(targets),
        )
        .order_by(InboundMessage.id.desc())
    ).first()


def _verify_bid_token(bid: MarketBid, data: dict[str, Any], now: datetime):
    expires_at = _parse_instant(data.get("expiresAt"))
    if expires_at is None:
        raise SwapTokenError(
            "SWAP_TOKEN_INVALID_EXPIRY", "expiresAt ausente o sin zona horaria"
        )
    if expires_at <= now:
        raise SwapTokenError("SWAP_TOKEN_EXPIRED", "expiresAt ya venció")

    token = data.get("swapToken")
    offer_id = data.get("offerId")
    if not isinstance(offer_id, str) or not offer_id:
        raise SwapTokenError("SWAP_TOKEN_MEMO_MISMATCH", "offerId ausente")

    return verify_swap_token(
        token,
        swap_id=bid.swap_id,
        proposer=bid.counterpart_city_id,
        give_asset="KWH",
        give_amount=Decimal(str(bid.quantity)),
        take_asset="CR",
        take_amount=_decimal(data.get("amount")),
        memo=offer_id,
        now=now,
    )


def _new_acceptor_record(
    *,
    bid: MarketBid,
    data: dict[str, Any],
    claims: dict[str, Any],
    now: datetime,
) -> SwapRecord:
    key = str(uuid.uuid4())
    return SwapRecord(
        # Para un aceptante, la operación durable es la aceptación: su idpk
        # local y su Idempotency-Key son la misma llave.
        idpk=key,
        swap_id=bid.swap_id,
        cycle_id=bid.cycle_id,
        role="ACCEPTOR",
        counterpart_city_id=bid.counterpart_city_id,
        # El proponente entrega KWH y pide CR; nosotros hacemos lo inverso.
        give_asset=claims["take"]["asset"],
        give_amount=_decimal(claims["take"]["amount"]),
        take_asset=claims["give"]["asset"],
        take_amount=_decimal(claims["give"]["amount"]),
        memo=None,
        status="SUBMITTED",
        idempotency_key=key,
        accept_idempotency_key=key,
        swap_token=data["swapToken"],
        expires_at=_parse_instant(data.get("expiresAt")),
        source_bid_idpk=bid.idpk,
        created_at=now,
    )


def _rejected_record(
    *,
    bid: MarketBid,
    data: dict[str, Any],
    now: datetime,
    reason: str,
) -> SwapRecord:
    key = str(uuid.uuid4())
    return SwapRecord(
        idpk=key,
        swap_id=bid.swap_id,
        cycle_id=bid.cycle_id,
        role="ACCEPTOR",
        counterpart_city_id=bid.counterpart_city_id,
        give_asset="CR",
        give_amount=_decimal(data.get("amount")),
        take_asset="KWH",
        take_amount=Decimal(str(bid.quantity)),
        memo=None,
        status="REJECTED",
        rejection_reason=reason,
        idempotency_key=key,
        accept_idempotency_key=None,
        swap_token=data.get("swapToken"),
        expires_at=_parse_instant(data.get("expiresAt")),
        source_bid_idpk=bid.idpk,
        accept_error=reason,
        created_at=now,
    )


async def _accept_record(
    session: Session,
    record: SwapRecord,
    service: WalletService,
    now: datetime,
) -> str:
    """Intenta aceptar un registro durable y clasifica el resultado."""

    record.accept_attempts += 1
    record.accept_attempted_at = now
    # Centinela durable: si el proceso muere durante el POST, el próximo tick
    # reintenta con la misma llave (la wallet deduplica por Idempotency-Key).
    record.accept_error = "WALLET_IN_FLIGHT"
    record.status = "ACCEPT_SUBMITTED"
    session.add(record)
    session.commit()
    session.refresh(record)

    try:
        response = await service.execute_accept_swap(session, record)
    except WalletTimeoutError as exc:
        record.accept_error = "WALLET_TIMEOUT"
        session.add(record)
        session.commit()
        logger.warning(
            "Timeout aceptando swap %s; se reintentará con la misma llave: %s",
            record.swap_id, exc,
        )
        return "retry"
    except WalletRateLimitError as exc:
        record.accept_error = f"WALLET_RATE_LIMIT:{exc.retry_after}"
        session.add(record)
        session.commit()
        logger.warning(
            "Rate limit aceptando swap %s (Retry-After=%s)",
            record.swap_id, exc.retry_after,
        )
        return "retry"
    except WalletError as exc:
        record.accept_error = f"WALLET_ERROR:{exc.status_code}"
        if exc.status_code in RETRYABLE_WALLET_STATUSES or exc.status_code is None:
            session.add(record)
            session.commit()
            logger.warning(
                "Error reintentable aceptando swap %s: %s",
                record.swap_id, exc,
            )
            return "retry"

        record.status = "REJECTED"
        record.rejection_reason = f"WALLET_{exc.status_code}"
        session.add(record)
        session.commit()
        logger.error(
            "Aceptación terminal rechazada para swap %s: %s",
            record.swap_id, exc,
        )
        return "rejected"

    if not response.accepted_for_processing:
        # La wallet contestó sin 202. No se declara liquidación: se conserva
        # la intención y se espera el evento final (AD5).
        logger.info(
            "Aceptación de swap %s respondió HTTP %s sin 202",
            record.swap_id, response.status_code,
        )

    record.status = "ACCEPT_SUBMITTED"
    record.accept_error = None
    session.add(record)
    session.commit()
    return "accepted"


async def accept_pending_swaps(
    session: Session,
    *,
    now: Optional[datetime] = None,
    wallet_service: Optional[WalletService] = None,
    limit: int = 25,
) -> dict[str, int]:
    """Procesa los `bid-accepted` pendientes de enviar a la wallet."""

    current = now or datetime.now(timezone.utc)
    service = wallet_service or WalletService()
    stats = {
        "scanned": 0,
        "accepted": 0,
        "retry": 0,
        "rejected": 0,
        "skipped": 0,
    }

    for bid in _accepted_bid_candidates(session, limit):
        stats["scanned"] += 1
        inbound = _bid_accept_payload(session, bid)
        if inbound is None:
            stats["skipped"] += 1
            logger.warning(
                "Bid %s está ACCEPTED pero no se encontró su bid-accepted",
                bid.idpk,
            )
            continue

        data = (inbound.payload or {}).get("data") or {}
        try:
            claims = _verify_bid_token(bid, data, current)
        except SwapTokenError as exc:
            if exc.reason in RETRYABLE_TOKEN_REASONS:
                # Una caída de JWKS no invalida el token: no se crea un
                # registro terminal y se reintenta en el próximo tick.
                stats["retry"] += 1
                logger.warning(
                    "No se pudo verificar swapToken para bid %s: %s",
                    bid.idpk, exc,
                )
                continue

            record = _rejected_record(
                bid=bid, data=data, now=current, reason=exc.reason,
            )
            session.add(record)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                stats["skipped"] += 1
                continue
            stats["rejected"] += 1
            logger.error(
                "swapToken inválido para bid %s (swap %s): %s",
                bid.idpk, bid.swap_id, exc,
            )
            continue

        record = _new_acceptor_record(
            bid=bid, data=data, claims=claims, now=current,
        )
        session.add(record)
        try:
            session.commit()
        except IntegrityError:
            # Otra réplica creó el registro para este bid entre la consulta y
            # el commit. La unicidad de source_bid_idpk es la última defensa.
            session.rollback()
            stats["skipped"] += 1
            logger.info(
                "Bid %s ya fue tomado por otra réplica", bid.idpk,
            )
            continue
        session.refresh(record)

    # Fase 2: intentos HTTP. Incluye reintentos de timeouts/429/5xx.
    for record in _accept_work(session, limit):
        outcome = await _accept_record(session, record, service, current)
        stats[outcome] += 1
        logger.info(
            "Swap %s aceptado localmente: record=%s resultado=%s intentos=%s",
            record.swap_id, record.idpk, outcome, record.accept_attempts,
        )

    return stats
