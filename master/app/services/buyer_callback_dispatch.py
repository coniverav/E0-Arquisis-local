"""E3-047: callbacks buyer enviados sólo tras hechos Wallet durables.

El consumidor E2 es el único escritor del ledger. Este servicio selecciona
SwapRecord terminales ya confirmados y reclama el task token con un lease;
nunca llama a AWS ni realiza un commit (ambos pertenecen al relay/caller).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import and_, or_
from sqlmodel import Session, select

from ..models import SagaExecution, SagaTaskToken, SwapRecord

BUYER_TERMINALS = frozenset({"SETTLED", "REJECTED", "EXPIRED", "CANCELLED"})


def _now(value: datetime | None) -> datetime:
    moment = value or datetime.now(timezone.utc)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("BUYER_CALLBACK_NOW_TIMEZONE_REQUIRED")
    return moment.astimezone(timezone.utc)


def claim_buyer_callbacks(
    session: Session, *, limit: int = 20, lease_seconds: int = 15,
    now: datetime | None = None,
) -> list[dict]:
    """SKIP LOCKED + lease recuperable; un callback por token y resultado DB.

    Un evento que llegó *antes* del task token también es visible al registrar
    la espera: no dependemos de que el evento dispare una llamada AWS directa.
    """
    if not 1 <= limit <= 100 or not 1 <= lease_seconds <= 60:
        raise ValueError("BUYER_CALLBACK_CLAIM_LIMIT_INVALID")
    moment = _now(now)
    rows = session.exec(
        select(SagaTaskToken, SagaExecution, SwapRecord)
        .join(SagaExecution, SagaExecution.id == SagaTaskToken.saga_execution_id)
        .join(SwapRecord, SwapRecord.idpk == SagaExecution.idpk)
        .where(
            SagaExecution.role == "BUYER",
            SagaExecution.status == "WAITING",
            SwapRecord.role == "ACCEPTOR",
            SwapRecord.status.in_(BUYER_TERMINALS),
            SwapRecord.swap_id == SagaExecution.swap_id,
            SagaTaskToken.swap_id == SwapRecord.swap_id,
            SagaTaskToken.correlation_idpk == SagaExecution.idpk,
            SagaTaskToken.wait_type == "TERMINAL",
            SagaTaskToken.timeout_at > moment,
            or_(
                and_(SagaTaskToken.status == "WAITING",
                     or_(SagaTaskToken.claim_until.is_(None),
                         SagaTaskToken.claim_until <= moment)),
                and_(SagaTaskToken.status == "DISPATCHING",
                     SagaTaskToken.claim_until <= moment),
            ),
        )
        .order_by(SagaTaskToken.id)
        .limit(limit)
        .with_for_update(of=SagaTaskToken, skip_locked=True)
    ).all()
    result: list[dict] = []
    for token, execution, swap in rows:
        claim = str(uuid4())
        token.status = "DISPATCHING"
        token.claim_token = claim
        token.claim_until = moment + timedelta(seconds=lease_seconds)
        session.add(token)
        result.append({
            "id": token.id,
            "claimToken": claim,
            "taskToken": token.task_token,
            "sagaExecutionId": execution.idpk,
            "swapId": swap.swap_id,
            "outcome": swap.status,
        })
    session.flush()
    return result


def finish_buyer_callback(
    session: Session, *, token_id: int, claim_token: str,
    outcome: str, retry_seconds: int = 2, now: datetime | None = None,
) -> bool:
    """Fencing por claim UUID; sólo AWS confirmado produce SUCCEEDED.

    AMBIGUOUS significa que AWS podría haber consumido el token, pero no
    tenemos confirmación: requiere conciliación, nunca éxito supuesto.
    """
    if outcome not in {"SUCCEEDED", "RETRY", "AMBIGUOUS"}:
        raise ValueError("BUYER_CALLBACK_RESULT_UNKNOWN")
    if not isinstance(claim_token, str) or not claim_token:
        raise ValueError("BUYER_CALLBACK_CLAIM_REQUIRED")
    if not 1 <= retry_seconds <= 60:
        raise ValueError("BUYER_CALLBACK_RETRY_INVALID")
    moment = _now(now)
    token = session.exec(
        select(SagaTaskToken).where(SagaTaskToken.id == token_id).with_for_update()
    ).one_or_none()
    if token is None or token.status != "DISPATCHING" or token.claim_token != claim_token:
        return False
    token.claim_token = None
    if outcome == "SUCCEEDED":
        token.status = "SUCCEEDED"
        token.claim_until = None
        token.completed_at = moment
    elif outcome == "RETRY":
        token.status = "WAITING"
        token.claim_until = moment + timedelta(seconds=retry_seconds)
    else:
        token.status = "AMBIGUOUS"
        token.claim_until = None
        token.completed_at = moment
    session.add(token)
    session.flush()
    return True
