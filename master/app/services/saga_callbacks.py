"""Correlación durable entre hechos Wallet y esperas de Step Functions."""

from __future__ import annotations

import logging
import os
from typing import Any

from sqlmodel import Session, select

from app.models import InboundMessage, SagaExecution, SagaTaskToken, SwapRecord
from app.services.message_audit import INBOUND_PROCESSED
from app.services.saga_tokens import (
    AmbiguousSagaTokenError,
    SagaCorrelationError,
    attach_swap_id,
    complete_task_token,
    find_waiting_task_token,
)


logger = logging.getLogger(__name__)

WALLET_CALLBACK_TYPES = frozenset({
    "swap-proposed",
    "swap-settled",
    "swap-rejected",
    "swap-expired",
    "swap-cancelled",
})

OUTCOMES = {
    "swap-proposed": "PROPOSED",
    "swap-settled": "SETTLED",
    "swap-rejected": "REJECTED",
    "swap-expired": "EXPIRED",
    "swap-cancelled": "CANCELLED",
}

WAIT_TYPES = {
    "swap-proposed": "PROPOSAL",
    "swap-settled": "TERMINAL",
    "swap-rejected": "TERMINAL",
    "swap-expired": "TERMINAL",
    "swap-cancelled": "TERMINAL",
}

SAGA_ROLES_BY_SWAP_ROLE = {
    "PROPOSER": "SELLER",
    "ACCEPTOR": "BUYER",
}


def callbacks_enabled() -> bool:
    return os.getenv("E3_SAGA_CALLBACKS_ENABLED", "false").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _swap_record(
    session: Session,
    *,
    operation_idpk: str | None,
    swap_id: str | None,
) -> SwapRecord | None:
    record = None
    if swap_id:
        record = session.exec(
            select(SwapRecord).where(SwapRecord.swap_id == swap_id)
        ).first()
    if record is None and operation_idpk:
        record = session.exec(select(SwapRecord).where(
            (SwapRecord.idpk == operation_idpk)
            | (SwapRecord.idempotency_key == operation_idpk)
            | (SwapRecord.accept_idempotency_key == operation_idpk)
        )).first()
    return record


def resolve_wallet_callback(
    session: Session,
    *,
    envelope_idpk: str,
    event_type: str,
    data: dict[str, Any],
    rejection_reason: str | None = None,
) -> dict[str, Any] | None:
    """Entrega el token sólo después de comprobar un efecto ya confirmado."""

    if event_type not in WALLET_CALLBACK_TYPES:
        return None
    processed = session.exec(select(InboundMessage.id).where(
        InboundMessage.idpk == envelope_idpk,
        InboundMessage.message_type == event_type,
        InboundMessage.status == INBOUND_PROCESSED,
    ).limit(1)).first()
    if processed is None:
        return None

    operation_idpk = data.get("idpk")
    swap_id = data.get("swapId") or data.get("id")
    operation_idpk = operation_idpk if isinstance(operation_idpk, str) else None
    swap_id = swap_id if isinstance(swap_id, str) else None
    if operation_idpk is None and swap_id is None:
        return None

    record = _swap_record(
        session,
        operation_idpk=operation_idpk,
        swap_id=swap_id,
    )
    if record is None:
        return None
    expected_role = SAGA_ROLES_BY_SWAP_ROLE.get(record.role)
    if expected_role is None:
        logger.warning(
            "Callback Wallet para rol de swap no soportado: event=%s record=%s",
            event_type,
            record.role,
        )
        return None
    try:
        token = find_waiting_task_token(
            session,
            correlation_idpk=operation_idpk,
            swap_id=swap_id,
            role=expected_role,
            wait_type=WAIT_TYPES[event_type],
        )
    except AmbiguousSagaTokenError:
        # No se adivina entre dos ejecuciones: el timeout/reconciliador debe
        # resolver la inconsistencia sin reanudar la saga equivocada.
        logger.exception(
            "Más de un callback Wallet coincide con event=%s swapId=%s",
            event_type,
            swap_id,
        )
        return None
    if token is None:
        return None

    execution = session.get(SagaExecution, token.saga_execution_id)
    if execution is None or execution.role != expected_role:
        return None

    if swap_id:
        try:
            attach_swap_id(session, execution, swap_id=swap_id)
        except SagaCorrelationError:
            logger.exception("swapId incompatible con la ejecución de saga")
            return None
        if token.swap_id is None:
            token.swap_id = swap_id
            session.add(token)
        elif token.swap_id != swap_id:
            return None

    output: dict[str, Any] = {"outcome": OUTCOMES[event_type]}
    if swap_id:
        output["swapId"] = swap_id
    if isinstance(data.get("expiresAt"), str):
        output["expiresAt"] = data["expiresAt"]
    if event_type == "swap-rejected" and rejection_reason:
        output["reason"] = rejection_reason

    # No se retorna el payload Wallet: puede contener JWT/swapToken/balances.
    return {
        "mode": "SUCCESS",
        "taskToken": token.task_token,
        "receipt": token.task_token_hash,
        "output": output,
        "correlationIdpk": operation_idpk,
        "swapId": swap_id,
    }


def complete_callback_delivery(
    session: Session,
    *,
    receipt: str,
    status: str,
    callback_idpk: str | None,
) -> tuple[SagaTaskToken | None, bool]:
    normalized = status.upper()
    if normalized not in {"SUCCEEDED", "FAILED"}:
        raise ValueError("Estado de callback no soportado")
    token = session.exec(select(SagaTaskToken).where(
        SagaTaskToken.task_token_hash == receipt
    )).one_or_none()
    if token is None:
        return None, False
    changed = complete_task_token(
        session,
        token,
        status=normalized,
        callback_idpk=callback_idpk,
    )
    return token, changed
