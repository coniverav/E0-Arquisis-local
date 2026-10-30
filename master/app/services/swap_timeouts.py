"""E2-055: expiración de swaps y bids cuyos plazos ya vencieron.

Reglas:

- Un HOLD solo se libera con evidencia. Si la wallet confirma que el swap
  terminó (`expired`, `cancelled`, `rejected`), se libera; si sigue vivo o
  no responde, se reintenta en el siguiente tick.
- Un swap que nunca llegó a la wallet puede expirar localmente al vencer su
  `proposal_expires_at`, sin consultar.
- Un `swap-settled` tardío no recompromete una reserva ya liberada; el hecho
  se registra en el ledger y la diferencia queda para conciliación.
- El barrido es idempotente: solo actúa sobre estados no terminales.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import and_, exists, or_
from sqlmodel import Session, select

from app.config import SWAP_TIMEOUT_GRACE_SECONDS
from app.models import (
    Cycle,
    InboundMessage,
    MarketBid,
    OutboundMessage,
    SwapRecord,
)
from app.services.message_audit import INBOUND_PROCESSED
from app.services.wallet_client import WalletClient, WalletError
from app.services.wallet_service import WalletService

logger = logging.getLogger(__name__)

TERMINAL_WALLET_STATUSES = {"expired", "cancelled", "rejected"}
WALLET_TERMINAL_EVENT = {
    "expired": "swap-expired",
    "cancelled": "swap-cancelled",
    "rejected": "swap-rejected",
}


def _utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def _parse_instant(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _set_bid_expired(session: Session, *, idpk: str | None) -> bool:
    if not idpk:
        return False
    bid = session.exec(
        select(MarketBid).where(MarketBid.idpk == idpk)
    ).first()
    if bid is None or bid.status in {"EXPIRED", "REJECTED", "SETTLED"}:
        return False
    bid.status = "EXPIRED"
    session.add(bid)
    return True


def _expire_record(
    session: Session,
    *,
    record: SwapRecord,
    reason: str,
) -> bool:
    """Marca el swap y su bid como EXPIRED y libera el HOLD local."""

    if record.status in {"SETTLED", "EXPIRED", "CANCELLED", "REJECTED"}:
        return False

    record.status = "EXPIRED"
    record.rejection_reason = reason
    session.add(record)

    service = WalletService()
    if record.memo:
        service._update_offer_and_reservation(
            session,
            offer_id=record.memo,
            bid_idpk=record.idpk,
            action="RELEASE",
        )

    bid_idpk = (
        record.idpk if record.role == "PROPOSER" else record.source_bid_idpk
    )
    _set_bid_expired(session, idpk=bid_idpk)
    session.flush()
    return True


def _accepted_bid_expiry(session: Session, bid: MarketBid) -> datetime | None:
    """Obtiene `expiresAt` del `bid-accepted` que originó la aceptación."""

    targets = list(session.exec(
        select(OutboundMessage.msg_id).where(
            OutboundMessage.idpk == bid.idpk,
            OutboundMessage.message_type == "bid",
        )
    ).all())
    if not targets:
        return None

    target_expr = InboundMessage.payload["data"]["target"].astext
    message = session.exec(
        select(InboundMessage)
        .where(
            InboundMessage.message_type == "bid-accepted",
            InboundMessage.status == INBOUND_PROCESSED,
            target_expr.in_(targets),
        )
        .order_by(InboundMessage.id.desc())
    ).first()
    if message is None:
        return None
    data = (message.payload or {}).get("data") or {}
    return _parse_instant(data.get("expiresAt"))


def _stale_records(session: Session, now: datetime, limit: int) -> list[SwapRecord]:
    return list(session.exec(
        select(SwapRecord)
        .where(
            SwapRecord.status.in_(["SUBMITTED", "PROPOSED", "ACCEPT_SUBMITTED"]),
            or_(
                SwapRecord.proposal_blocked_at.is_not(None),
                and_(
                    SwapRecord.expires_at.is_not(None),
                    SwapRecord.expires_at <= now,
                ),
                and_(
                    SwapRecord.proposal_expires_at.is_not(None),
                    SwapRecord.proposal_expires_at <= now,
                ),
            ),
        )
        .order_by(SwapRecord.id)
        .limit(limit)
    ).all())


def _stale_accepted_bids(session: Session, now: datetime) -> list[MarketBid]:
    """Bids ACCEPTED sin SwapRecord cuyo `expiresAt` ya venció."""

    linked = exists(
        select(SwapRecord.id).where(SwapRecord.source_bid_idpk == MarketBid.idpk)
    )
    bids = session.exec(
        select(MarketBid)
        .where(
            MarketBid.direction == "OUTGOING",
            MarketBid.status == "ACCEPTED",
            ~linked,
        )
        .order_by(MarketBid.id)
    ).all()

    stale: list[MarketBid] = []
    for bid in bids:
        expires_at = _accepted_bid_expiry(session, bid)
        if expires_at is not None and expires_at <= now:
            stale.append(bid)
    return stale


def _stale_pending_bids(session: Session, now: datetime) -> list[MarketBid]:
    """OUTGOING PENDING sin respuesta después de `lastBidAt` + gracia."""

    rows = session.exec(
        select(MarketBid, Cycle)
        .join(Cycle, Cycle.cycle_id == MarketBid.cycle_id)
        .where(
            MarketBid.direction == "OUTGOING",
            MarketBid.status == "PENDING",
            Cycle.market_last_bid_at.is_not(None),
        )
        .order_by(MarketBid.id)
    ).all()

    stale: list[MarketBid] = []
    for bid, cycle in rows:
        if _utc(cycle.market_last_bid_at) + timedelta(
            seconds=SWAP_TIMEOUT_GRACE_SECONDS
        ) <= now:
            stale.append(bid)
    return stale


async def expire_stale_swaps(
    session: Session,
    *,
    now: datetime | None = None,
    client: WalletClient | None = None,
    limit: int = 50,
) -> dict[str, int]:
    """Barrido idempotente de swaps/bids vencidos.

    La consulta HTTP ocurre antes de mutar estados, pero dentro de la misma
    sesión de trabajo. El caller decide el commit.
    """

    now = _utc(now or datetime.now(timezone.utc))
    client = client or WalletClient()

    stats = {
        "expired_swaps": 0,
        "expired_bids": 0,
        "released_holds": 0,
        "wallet_checked": 0,
        "settled_from_wallet": 0,
        "cancel_requested": 0,
        "indeterminate": 0,
    }

    for record in _stale_records(session, now, limit):
        # E2-068: timeout HTTP NO prueba ausencia de operación en wallet.
        # Sólo puede declararse NEVER_SENT si nunca hubo intento POST.
        # Si hubo intento y falta swapId, conservar HOLD y esperar evento o
        # conciliación; jamás generar un segundo swap con idpk nuevo.
        if (
            record.role == "PROPOSER"
            and record.swap_id is None
            and record.proposal_attempt_count == 0
            and record.proposal_http_accepted_at is None
            and record.proposal_blocked_at is None
        ):
            if _expire_record(
                session, record=record, reason="PROPOSAL_NEVER_SENT"
            ):
                stats["expired_swaps"] += 1
                if record.memo:
                    stats["released_holds"] += 1
            continue

        if not record.swap_id:
            stats["indeterminate"] += 1
            continue

        stats["wallet_checked"] += 1
        try:
            wallet_swap = await client.get_swap(record.swap_id)
        except WalletError as exc:
            # Un 404 confirma que la wallet no conoce el swap. Si el POST ya
            # había sido bloqueado de forma determinista, la energía puede
            # liberarse.
            if (
                record.proposal_blocked_at is not None
                and getattr(exc, "status_code", None) == 404
            ):
                if _expire_record(
                    session, record=record, reason="WALLET_SWAP_NOT_FOUND"
                ):
                    stats["expired_swaps"] += 1
                    if record.memo:
                        stats["released_holds"] += 1
                continue
            logger.warning(
                "No se pudo confirmar swap %s para expirar: %s",
                record.swap_id,
                exc,
            )
            stats["indeterminate"] += 1
            continue

        status = str((wallet_swap or {}).get("status") or "").lower()

        if status == "settled":
            # Hecho real: aplicar el mismo flujo que un evento, sin liberar
            # una reserva que la wallet ya confirmó.
            WalletService().process_swap_event(
                session,
                event_type="swap-settled",
                data={"swapId": record.swap_id, "idpk": record.idpk},
            )
            stats["settled_from_wallet"] += 1
            continue

        if status in TERMINAL_WALLET_STATUSES:
            if _expire_record(
                session,
                record=record,
                reason=f"WALLET_{status.upper()}",
            ):
                stats["expired_swaps"] += 1
                if record.memo:
                    stats["released_holds"] += 1
            continue

        if status in {"proposed", "accepted"}:
            # La wallet todavía lo considera vivo. No liberar a ciegas.
            if (
                record.expires_at is not None
                and _utc(record.expires_at) <= now
                and record.swap_id
            ):
                try:
                    await client.cancel_swap(
                        record.swap_id,
                        # Cancelar es otra operación: una llave UUID estable,
                        # distinta de la usada en POST /swaps, también tras reinicio.
                        idempotency_key=str(uuid5(
                            NAMESPACE_URL,
                            f"energyshark:cancel:{record.idempotency_key}:{record.swap_id}",
                        )),
                    )
                    stats["cancel_requested"] += 1
                except WalletError as exc:
                    logger.warning(
                        "No se pudo cancelar swap vencido %s: %s",
                        record.swap_id,
                        exc,
                    )
                    stats["indeterminate"] += 1
            continue

        stats["indeterminate"] += 1

    for bid in _stale_accepted_bids(session, now):
        bid.status = "EXPIRED"
        session.add(bid)
        stats["expired_bids"] += 1

    for bid in _stale_pending_bids(session, now):
        if bid.status == "PENDING":
            bid.status = "EXPIRED"
            session.add(bid)
            stats["expired_bids"] += 1

    session.flush()
    return stats
