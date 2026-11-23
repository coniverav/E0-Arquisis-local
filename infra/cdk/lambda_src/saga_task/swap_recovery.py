"""E3-048: reconciliación Wallet de sagas, sin efectos económicos en Lambda.

GET /swaps verifica swapId y estado; cancelación reutiliza la misma llave
que la intención seller y SIEMPRE consulta Wallet después del POST.
Ni HTTP 200/202 ni el timeout de Step Functions sustituyen el evento durable
Wallet que Master aplica al ledger y a las reservas.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable
from uuid import NAMESPACE_URL, uuid5

from errors import WalletRetryable


class SagaRecoveryInconsistent(ValueError):
    """Identidades incompatibles; ninguna solicitud Wallet debe salir."""


class SagaRecoveryPending(RuntimeError):
    """El estado económico de Wallet es ambiguo o todavía no terminal."""


TERMINAL = frozenset({"SETTLED", "REJECTED", "EXPIRED", "CANCELLED"})
VISIBLE = TERMINAL | {"PROPOSED", "ACCEPTED"}


def _require(valid: bool, code: str) -> None:
    if not valid:
        raise SagaRecoveryInconsistent(code)


def cancel_key_for_seller(saga_execution_id: str) -> str:
    _require(isinstance(saga_execution_id, str) and bool(saga_execution_id),
             "RECOVERY_SAGA_REQUIRED")
    return str(uuid5(NAMESPACE_URL,
                     f"energyshark:seller:cancel:{saga_execution_id}"))


def _persisted_identity(
    store: Any, *, saga_execution_id: str, role: str,
    swap_id: str | None = None, idempotency_key: str | None = None,
    cancel_key: str | None = None,
) -> dict[str, Any]:
    """Una lectura DB comprueba rol, ciclo, operación y llaves persistidas."""
    _require(isinstance(saga_execution_id, str) and bool(saga_execution_id),
             "RECOVERY_SAGA_REQUIRED")
    _require(role in {"SELLER", "BUYER"}, "RECOVERY_ROLE_INVALID")
    _require(swap_id is None or isinstance(swap_id, str) and bool(swap_id),
             "RECOVERY_SWAP_ID_INVALID")
    with store.connect(store.dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """SELECT se.role, se.cycle_id, se.swap_id, se.status,
                          sr.idpk, sr.cycle_id, sr.role, sr.swap_id,
                          sr.idempotency_key, sr.accept_idempotency_key,
                          sr.expires_at
                   FROM saga_executions se
                   JOIN swap_records sr ON sr.idpk = se.idpk
                   WHERE se.idpk = %s""",
                (saga_execution_id,),
            )
            row = cursor.fetchone()
    _require(row is not None, "RECOVERY_OPERATION_UNKNOWN")
    (saga_role, saga_cycle, saga_swap, saga_status,
     record_idpk, record_cycle, record_role, record_swap,
     proposal_key, accept_key, expiry) = row
    expected_record_role = "PROPOSER" if role == "SELLER" else "ACCEPTOR"
    _require(
        saga_role == role and record_role == expected_record_role
        and saga_cycle == record_cycle and isinstance(record_cycle, str)
        and record_idpk == saga_execution_id
        and proposal_key == saga_execution_id
        and (role != "BUYER" or accept_key == saga_execution_id)
        and (saga_swap is None and role == "SELLER" or saga_swap == record_swap)
        and isinstance(record_swap, str) and bool(record_swap)
        and (swap_id is None or swap_id == record_swap),
        "RECOVERY_ECONOMIC_IDENTITY_MISMATCH",
    )
    if idempotency_key is not None:
        _require(role == "SELLER" and idempotency_key == proposal_key,
                 "RECOVERY_PROPOSAL_KEY_MISMATCH")
    if cancel_key is not None:
        _require(role == "SELLER" and cancel_key == cancel_key_for_seller(saga_execution_id),
                 "RECOVERY_CANCEL_KEY_MISMATCH")
    return {"swapId": record_swap, "sagaExecutionId": saga_execution_id,
            "expiresAt": expiry}


def _wallet_status(wallet: Any, *, swap_id: str,
                   remaining_seconds: Callable[[], float]) -> dict[str, Any]:
    result = wallet.get_swap(swap_id=swap_id, deadline_seconds=remaining_seconds())
    if result.status_code != 200:
        raise SagaRecoveryPending("RECOVERY_GET_NOT_AUTHORITATIVE")
    data = result.data
    _require(isinstance(data, dict) and data.get("swapId") == swap_id,
             "RECOVERY_WALLET_SWAP_ID_MISMATCH")
    status = data.get("status")
    if not isinstance(status, str) or status.upper() not in VISIBLE:
        raise SagaRecoveryPending("RECOVERY_WALLET_STATUS_UNKNOWN")
    answer: dict[str, Any] = {"status": status.upper(), "swapId": swap_id}
    expires_at = data.get("expiresAt")
    if isinstance(expires_at, str):
        try:
            moment = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            if moment.tzinfo is not None and moment.utcoffset() is not None:
                answer["expiresAt"] = moment.astimezone(timezone.utc).isoformat()
        except ValueError:
            pass
    # Jamás enviar token, lastEvent, saldos ni API secrets a Step Functions.
    return answer


def reconcile_swap(
    store: Any, wallet: Any, *, saga_execution_id: str, role: str,
    swap_id: str | None, idempotency_key: str | None,
    remaining_seconds: Callable[[], float],
) -> dict[str, Any]:
    identity = _persisted_identity(
        store, saga_execution_id=saga_execution_id, role=role,
        swap_id=swap_id, idempotency_key=idempotency_key,
    )
    result = _wallet_status(wallet, swap_id=identity["swapId"],
                            remaining_seconds=remaining_seconds)
    if result["status"] == "PROPOSED" and "expiresAt" not in result:
        stored = identity["expiresAt"]
        if isinstance(stored, datetime) and stored.tzinfo is not None:
            result["expiresAt"] = stored.astimezone(timezone.utc).isoformat()
    if result["status"] == "PROPOSED" and "expiresAt" not in result:
        raise SagaRecoveryPending("RECOVERY_PROPOSAL_EXPIRY_UNKNOWN")
    return result


def cancel_seller_and_recheck(
    store: Any, wallet: Any, *, saga_execution_id: str,
    swap_id: str, idempotency_key: str,
    remaining_seconds: Callable[[], float],
) -> dict[str, Any]:
    """GET → POST cancelación si vivo → GET; no libera HOLD ni escribe ledger."""
    identity = _persisted_identity(
        store, saga_execution_id=saga_execution_id, role="SELLER",
        swap_id=swap_id, cancel_key=idempotency_key,
    )
    before = _wallet_status(wallet, swap_id=identity["swapId"],
                            remaining_seconds=remaining_seconds)
    if before["status"] in TERMINAL:
        return before
    try:
        # Misma Idempotency-Key que seller_saga_intent en cada replay.
        wallet.cancel(swap_id=swap_id, idempotency_key=idempotency_key,
                      deadline_seconds=remaining_seconds())
    except WalletRetryable:
        # Un timeout puede ocurrir DESPUÉS de aceptar el POST. Consultar antes
        # de volver a pedir cancelación; nunca interpretar error como rechazo.
        after = _wallet_status(wallet, swap_id=swap_id,
                               remaining_seconds=remaining_seconds)
        if after["status"] in TERMINAL:
            return after
        raise
    after = _wallet_status(wallet, swap_id=swap_id,
                           remaining_seconds=remaining_seconds)
    if after["status"] not in TERMINAL:
        raise SagaRecoveryPending("RECOVERY_CANCEL_NOT_TERMINAL")
    return after
