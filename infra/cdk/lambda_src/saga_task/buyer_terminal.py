"""E3-047: cierre buyer consistente con estado Wallet y ledger ya persistidos.

Esta Lambda jamás escribe ledger; el único dueño económico es Master al
aplicar el mensaje Wallet. Rechazos locales pre-HTTP no representan settlement.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping


class BuyerTerminalInconsistent(RuntimeError):
    """Resultado solicitado incompatible con las filas económicas."""


class BuyerTerminalPending(RuntimeError):
    """No hay hecho Wallet suficiente para declarar un estado final."""


ACTIVE = frozenset({"PENDING_START", "RUNNING", "WAITING"})
TERMINAL = frozenset({"SETTLED", "REJECTED", "EXPIRED", "CANCELLED"})
LOCAL_REJECTIONS = frozenset({"INVALID_SWAP_TOKEN", "DEADLINE_EXPIRED"})


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise BuyerTerminalInconsistent(reason)


def finalize_buyer(store: Any, *, saga_execution_id: str,
                   result: Mapping[str, Any]) -> dict[str, Any]:
    _require(isinstance(saga_execution_id, str) and bool(saga_execution_id),
             "BUYER_TERMINAL_EXECUTION_REQUIRED")
    _require(isinstance(result, Mapping), "BUYER_TERMINAL_RESULT_REQUIRED")
    target = result.get("status")
    reason = result.get("reason")
    _require(target in {"SETTLED", "REJECTED", "TIMED_OUT"},
             "BUYER_TERMINAL_STATUS_INVALID")
    moment = store.now()
    _require(isinstance(moment, datetime) and moment.tzinfo is not None
             and moment.utcoffset() is not None, "BUYER_TERMINAL_NOW_INVALID")
    moment = moment.astimezone(timezone.utc)
    with store.connect(store.dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """SELECT id, role, status, cycle_id, swap_id
                   FROM saga_executions WHERE idpk = %s FOR UPDATE""",
                (saga_execution_id,),
            )
            execution = cursor.fetchone()
            _require(execution is not None and execution[1] == "BUYER",
                     "BUYER_TERMINAL_SAGA_UNKNOWN")
            execution_id, _, current, cycle_id, saga_swap = execution
            cursor.execute(
                """SELECT sr.id, sr.role, sr.status, sr.cycle_id, sr.swap_id,
                          sr.idpk, sr.accept_idempotency_key, sr.accept_attempts,
                          b.direction, b.status, b.cycle_id, b.swap_id
                   FROM swap_records sr
                   JOIN market_bids b ON b.idpk = sr.source_bid_idpk
                   WHERE sr.idpk = %s FOR UPDATE OF sr""",
                (saga_execution_id,),
            )
            row = cursor.fetchone()
            _require(row is not None, "BUYER_TERMINAL_SWAP_UNKNOWN")
            (record_id, role, wallet_status, swap_cycle, swap_id,
             record_idpk, accept_key, attempts, bid_dir, bid_status,
             bid_cycle, bid_swap) = row
            _require(
                role == "ACCEPTOR" and record_idpk == accept_key == saga_execution_id
                and swap_id == saga_swap == bid_swap
                and cycle_id == swap_cycle == bid_cycle
                and bid_dir == "OUTGOING" and bid_status == "ACCEPTED",
                "BUYER_TERMINAL_ECONOMIC_IDENTITY_MISMATCH",
            )
            if target == "SETTLED":
                if wallet_status != "SETTLED":
                    raise BuyerTerminalPending("BUYER_TERMINAL_SETTLEMENT_NOT_PERSISTED")
            elif target == "REJECTED":
                if wallet_status == "SETTLED":
                    raise BuyerTerminalInconsistent("BUYER_TERMINAL_REJECT_AFTER_SETTLED")
                if wallet_status not in {"REJECTED", "EXPIRED", "CANCELLED"}:
                    # Rechazo local válido SOLO antes de cualquier POST Wallet.
                    if reason not in LOCAL_REJECTIONS or wallet_status != "SUBMITTED" or attempts != 0:
                        raise BuyerTerminalPending("BUYER_TERMINAL_REJECTION_NOT_CONFIRMED")
                    cursor.execute(
                        """UPDATE swap_records SET status = 'REJECTED', rejection_reason = %s
                           WHERE id = %s AND status = 'SUBMITTED' AND accept_attempts = 0""",
                        (reason, record_id),
                    )
                    _require(cursor.rowcount == 1, "BUYER_TERMINAL_LOCAL_REJECTION_RACE")
            else:
                # TIMED_OUT no constituye una liquidación ni un rechazo Wallet.
                # Si hay hecho terminal, debe elegirse SETTLED/REJECTED.
                if wallet_status in TERMINAL:
                    raise BuyerTerminalInconsistent("BUYER_TERMINAL_TIMEOUT_HAS_WALLET_FACT")
            if current == target:
                return {"status": target, "alreadyFinalized": True, "swapId": swap_id}
            _require(current in ACTIVE, "BUYER_TERMINAL_ALREADY_DIFFERENT")
            cursor.execute(
                """UPDATE saga_executions
                   SET status = %s, updated_at = %s, terminal_at = %s
                   WHERE id = %s""",
                (target, moment, moment, execution_id),
            )
            _require(cursor.rowcount == 1, "BUYER_TERMINAL_SAGA_UPDATE_FAILED")
            cursor.execute(
                """UPDATE saga_task_tokens
                   SET status = 'SUPERSEDED', completed_at = %s
                   WHERE saga_execution_id = %s AND status = 'WAITING'""",
                (moment, execution_id),
            )
        connection.commit()
    return {"status": target, "alreadyFinalized": False, "swapId": swap_id}
