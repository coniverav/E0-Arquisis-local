"""E2-032: despacho durable de propuestas de swap de vendedor hacia Wallet.

E2-031 deja en la misma transacción MarketBid(RESERVED),
OfferReservation(HOLD) y SwapRecord(SUBMITTED). Este worker toma esa intención,
termina el claim PostgreSQL *antes* del POST externo y reutiliza siempre
SwapRecord.idempotency_key. No envía bid-accepted: solo swap-proposed confirma
que Wallet ha propuesto la operación (E2-033).

Dos workers pueden coexistir. SELECT FOR UPDATE SKIP LOCKED + lease impiden
la misma llamada simultánea en condiciones normales; una caída/lease vencida
puede causar un reintento, pero la misma Idempotency-Key preserva la operación.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import logging
from uuid import UUID, uuid4

from sqlalchemy import or_
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from ..config import CITY_ID, WALLET_TIMEOUT_SECONDS
from ..database import engine as default_engine
from ..models import MarketBid, MarketOffer, OfferReservation, SwapRecord
from .wallet_recovery import (inspect_known_swap, apply_wallet_observation, TERMINAL_STATES)  # E2-068
from .wallet_client import (
    WalletClient, WalletError, WalletRateLimitError,
    WalletTimeoutError, WalletSwapWriteResponse,
)

logger = logging.getLogger(__name__)

# La llamada HTTP normalmente expira antes del lease. Si el proceso cae, otro
# worker puede reclamar la misma operación usando exactamente la misma llave.
LEASE_SECONDS = max(30, int(WALLET_TIMEOUT_SECONDS * 3) + 10)
MAX_BATCH = 10


@dataclass(frozen=True)
class ProposalAttempt:
    idpk: str
    claim_token: str
    idempotency_key: str
    give_amount: Decimal
    take_amount: Decimal
    counterpart_city_id: str
    memo: str
    expires_at: datetime
    attempt_count: int


def _aware(now: datetime) -> datetime:
    if now.tzinfo is None:
        raise ValueError('now requiere zona horaria')
    return now.astimezone(timezone.utc)


def _claim_one(session: Session, *, now: datetime) -> ProposalAttempt | None:
    """Bloqueo no bloqueante, compartido entre réplicas; sin HTTP en la transacción."""
    record = session.exec(
        select(SwapRecord)
        .join(MarketBid, MarketBid.idpk == SwapRecord.idpk)
        .join(OfferReservation, OfferReservation.bid_idpk == SwapRecord.idpk)
        .join(MarketOffer, MarketOffer.offer_id == OfferReservation.offer_id)
        .where(
            SwapRecord.role == 'PROPOSER',
            SwapRecord.status == 'SUBMITTED',
            SwapRecord.give_asset == 'KWH',
            SwapRecord.take_asset == 'CR',
            SwapRecord.proposal_http_accepted_at.is_(None),
            SwapRecord.proposal_blocked_at.is_(None),
            # No enviar propuestas cuya caducidad durable ya pasó.
            or_(SwapRecord.proposal_expires_at.is_(None),
                SwapRecord.proposal_expires_at > now + timedelta(seconds=3)),
            or_(SwapRecord.proposal_retry_at.is_(None), SwapRecord.proposal_retry_at <= now),
            or_(SwapRecord.proposal_lease_until.is_(None), SwapRecord.proposal_lease_until <= now),
            MarketBid.direction == 'INCOMING',
            MarketBid.status == 'RESERVED',
            MarketBid.cycle_id == SwapRecord.cycle_id,
            MarketBid.offer_id == MarketOffer.offer_id,
            MarketBid.counterpart_city_id == SwapRecord.counterpart_city_id,
            OfferReservation.status == 'HOLD',
            OfferReservation.quantity == MarketBid.quantity,
            MarketOffer.offer_id == SwapRecord.memo,
            MarketOffer.cycle_id == SwapRecord.cycle_id,
            MarketOffer.is_local.is_(True),
            MarketOffer.city_id == CITY_ID,
        )
        .order_by(SwapRecord.created_at, SwapRecord.id)
        .limit(1)
        .with_for_update(of=SwapRecord, skip_locked=True)
    ).first()
    if record is None:
        return None

    # E2-031 creó esta clave como UUID y el contenido económico quedó congelado
    # en SwapRecord. NO recalcular usando el precio actual de la oferta.
    UUID(record.idempotency_key)
    if not record.memo or record.give_amount <= 0 or record.give_amount != record.give_amount.to_integral_value():
        raise ValueError('SWAP_INTENT_INVALID_KWH')
    if record.take_amount < 0 or record.take_amount != record.take_amount.quantize(Decimal('0.01')):
        raise ValueError('SWAP_INTENT_INVALID_CR')

    # Se congela el expiresAt antes del primer POST, también ante timeouts.
    if record.proposal_expires_at is None:
        record.proposal_expires_at = now + timedelta(seconds=55)
    token = str(uuid4())
    record.proposal_claim_token = token
    record.proposal_lease_until = now + timedelta(seconds=LEASE_SECONDS)
    record.proposal_attempt_count += 1
    session.add(record)
    session.flush()
    return ProposalAttempt(
        idpk=record.idpk,
        claim_token=token,
        idempotency_key=record.idempotency_key,
        give_amount=record.give_amount,
        take_amount=record.take_amount,
        counterpart_city_id=record.counterpart_city_id,
        memo=record.memo,
        expires_at=record.proposal_expires_at,
        attempt_count=record.proposal_attempt_count,
    )


def _finish(session: Session, attempt: ProposalAttempt, *, now: datetime,
            outcome: str, error_code: str | None = None,
            retry_after: int | None = None,
            confirmed_swap_id: str | None = None) -> None:
    record = session.exec(select(SwapRecord).where(
        SwapRecord.idpk == attempt.idpk,
    ).with_for_update()).first()
    if record is None or record.proposal_claim_token != attempt.claim_token:
        # Otro proceso recuperó el lease. No sobrescribir su resultado.
        return

    # E2-068: un evento wallet puede llegar mientras el POST está en vuelo.
    # No pisar PROPOSED/SETTLED ni reactivar un swap terminal por un timeout.
    if record.status in TERMINAL_STATES | {"PROPOSED"}:
        record.proposal_claim_token = None
        record.proposal_lease_until = None
        record.proposal_retry_at = None
        if outcome == 'ACCEPTED':
            record.proposal_http_accepted_at = now
        session.add(record)
        session.flush()
        return

    record.proposal_claim_token = None
    record.proposal_lease_until = None
    record.proposal_last_error = error_code
    if confirmed_swap_id and not record.swap_id:
        record.swap_id = confirmed_swap_id
    if outcome == 'ACCEPTED':
        record.proposal_http_accepted_at = now
        record.proposal_retry_at = None
        record.proposal_blocked_at = None
        # NO cambiar SwapRecord.status: HTTP 202 no implica swap-proposed.
    elif outcome == 'RECOVERED':
        record.proposal_retry_at = None
    elif outcome == 'BLOCKED':
        record.proposal_blocked_at = now
        record.proposal_retry_at = None
        # No liberar HOLD frente a una respuesta incierta o un error HTTP.
        # Requiere revisión/reconciliación antes de liberar la energía.
    elif outcome == 'RETRY':
        backoff = min(60, 2 ** min(attempt.attempt_count, 6))
        if retry_after is not None:
            backoff = max(backoff, max(1, retry_after))
        record.proposal_retry_at = now + timedelta(seconds=backoff)
    else:
        raise ValueError('Resultado de POST /swaps desconocido')
    session.add(record)
    session.flush()


async def dispatch_swap_proposals(
    *, limit: int = MAX_BATCH, client: WalletClient | None = None,
    db_engine: Engine | None = None, now: datetime | None = None,
) -> dict[str, int]:
    """Procesa hasta `limit` propuestas elegibles; sin llamadas HTTP bajo lock.

    Puede ejecutarse periódicamente en el worker. Siempre deja la reserva HOLD
    hasta que un evento definitivo de Wallet la confirme o libere.
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_BATCH:
        raise ValueError(f'limit debe estar entre 1 y {MAX_BATCH}')
    reference = _aware(now or datetime.now(timezone.utc))
    target_engine = db_engine or default_engine
    wallet = client or WalletClient()
    totals = {'claimed': 0, 'accepted_202': 0, 'retry': 0, 'blocked': 0, 'recovered': 0}

    for _ in range(limit):
        with Session(target_engine) as session:
            attempt = _claim_one(session, now=reference)
            session.commit()  # Se libera FOR UPDATE antes de hacer POST.
        if attempt is None:
            break
        totals['claimed'] += 1

        # E2-068: si un 202 se perdió y ya conocemos el swapId, consultar
        # Wallet ANTES de repetir el POST. Sin swapId solo procede reintentar
        # con la MISMA llave; no hay GET oficial por idempotency-key.
        if attempt.attempt_count > 1:
            with Session(target_engine) as recovery_session:
                current = recovery_session.exec(select(SwapRecord).where(
                    SwapRecord.idpk == attempt.idpk,
                )).first()
                known_swap_id = current.swap_id if current is not None else None
            if known_swap_id:
                observation = await inspect_known_swap(wallet, known_swap_id)
                if observation is not None:
                    with Session(target_engine) as recovery_session:
                        current = recovery_session.exec(select(SwapRecord).where(
                            SwapRecord.idpk == attempt.idpk,
                        ).with_for_update()).first()
                        if (current is not None
                                and current.proposal_claim_token == attempt.claim_token
                                and apply_wallet_observation(
                                    recovery_session, record=current,
                                    observation=observation, now=reference)):
                            _finish(recovery_session, attempt,
                                    now=_aware(now or datetime.now(timezone.utc)),
                                    outcome='RECOVERED')
                            recovery_session.commit()
                            totals['recovered'] += 1
                            continue
                        recovery_session.commit()

        outcome = 'RETRY'
        error_code = None
        retry_after = None
        confirmed_swap_id = None
        try:
            response: WalletSwapWriteResponse = await wallet.propose_swap(
                idempotency_key=attempt.idempotency_key,
                expires_at=attempt.expires_at,
                give_asset='KWH',
                give_amount=attempt.give_amount,
                take_asset='CR',
                take_amount=attempt.take_amount,
                counterpart_city_id=attempt.counterpart_city_id,
                memo=attempt.memo,
            )
            if response.status_code == 202 and response.accepted_for_processing:
                outcome = 'ACCEPTED'
                if isinstance(response.data, dict):
                    candidate = response.data.get('swapId')
                    if isinstance(candidate, str) and candidate:
                        confirmed_swap_id = candidate
            else:
                error_code = f'UNEXPECTED_HTTP_{response.status_code}'
        except WalletRateLimitError as exc:
            error_code = 'RATE_LIMIT_429'
            retry_after = exc.retry_after
        except WalletTimeoutError:
            error_code = 'TIMEOUT_OR_NETWORK'
        except WalletError as exc:
            code = exc.status_code
            error_code = f'WALLET_HTTP_{code}' if code is not None else 'WALLET_ERROR'
            retry_after = exc.retry_after
            if code is not None and 400 <= code < 500 and code != 429:
                # Posible rechazo permanente: bloquear envío, preservar HOLD
                # hasta reconciliar sin arriesgar energía duplicada.
                outcome = 'BLOCKED'
        except (ValueError, TypeError):
            error_code = 'MALFORMED_WALLET_RESPONSE'

        with Session(target_engine) as session:
            _finish(session, attempt, now=_aware(now or datetime.now(timezone.utc)),
                    outcome=outcome, error_code=error_code, retry_after=retry_after,
                    confirmed_swap_id=confirmed_swap_id)
            session.commit()

        if outcome == 'ACCEPTED':
            totals['accepted_202'] += 1
        elif outcome == 'RETRY':
            totals['retry'] += 1
        else:
            totals['blocked'] += 1
        logger.info('E2-032 wallet swap dispatch idpk=%s outcome=%s reason=%s',
                    attempt.idpk, outcome, error_code or 'HTTP_202')

    return totals
