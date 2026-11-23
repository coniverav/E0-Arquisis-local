"""E3-042: finalización seller tras el resultado económico persistido de Wallet.

NO aplica ledger, no cambia remaining, no publica oferta y no libera HOLD.
WalletService.process_swap_event conserva esas operaciones en una sola transacción
junto al hecho de Wallet; una Lambda posterior sólo verifica y cierra la saga.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any


class SellerTerminalPending(RuntimeError):
    """La autoridad Wallet todavía no confirmó un estado terminal."""


class SellerTerminalInconsistent(RuntimeError):
    """La información persistida no permite cerrar sin riesgo económico."""


TERMINAL_WALLET = frozenset({"SETTLED", "REJECTED", "EXPIRED", "CANCELLED"})
ACTIVE_SAGA = frozenset({"PENDING_START", "RUNNING", "WAITING"})
FINAL_SAGA = frozenset({"SETTLED", "REJECTED", "COMPENSATED", "TIMED_OUT", "FAILED"})


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise SellerTerminalInconsistent(code)


def _identity(bid_idpk: Any, saga_execution_id: Any, offer_id: Any = None) -> None:
    _require(isinstance(bid_idpk, str) and bool(bid_idpk), "SELLER_TERMINAL_BID_REQUIRED")
    _require(isinstance(saga_execution_id, str) and saga_execution_id == bid_idpk,
             "SELLER_TERMINAL_SAGA_IDENTITY_MISMATCH")
    if offer_id is not None:
        _require(isinstance(offer_id, str) and bool(offer_id),
                 "SELLER_TERMINAL_OFFER_REQUIRED")


def _now(store: Any) -> datetime:
    now = store.now()
    _require(isinstance(now, datetime) and now.tzinfo is not None
             and now.utcoffset() is not None, "SELLER_TERMINAL_NOW_INVALID")
    return now.astimezone(timezone.utc)


def _complete_seller(
    store: Any, *, bid_idpk: str, saga_execution_id: str,
    target: str, offer_id: str | None = None,
) -> dict[str, Any]:
    _identity(bid_idpk, saga_execution_id, offer_id)
    _require(target in {"SETTLED", "COMPENSATED", "REJECTED"},
             "SELLER_TERMINAL_TARGET_UNKNOWN")
    moment = _now(store)

    with store.connect(store.dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """SELECT id, role, cycle_id, swap_id, status FROM saga_executions
                   WHERE idpk = %s FOR UPDATE""", (bid_idpk,),
            )
            execution = cursor.fetchone()
            _require(execution is not None and execution[1] == "SELLER"
                     and isinstance(execution[2], str),
                     "SELLER_TERMINAL_EXECUTION_UNKNOWN")
            execution_pk, _, cycle_id, execution_swap, saga_status = execution
            _require(saga_status in ACTIVE_SAGA | FINAL_SAGA,
                     "SELLER_TERMINAL_SAGA_STATUS_INVALID")

            cursor.execute(
                """SELECT cycle_id, offer_id, counterpart_city_id, quantity,
                          status, direction, swap_id
                   FROM market_bids WHERE idpk = %s""", (bid_idpk,),
            )
            bid = cursor.fetchone()
            _require(bid is not None and bid[0] == cycle_id and bid[5] == "INCOMING"
                     and isinstance(bid[3], int) and bid[3] > 0,
                     "SELLER_TERMINAL_BID_MISMATCH")
            _, bid_offer, buyer, quantity, bid_status, _, bid_swap = bid
            if offer_id is not None:
                _require(bid_offer == offer_id, "SELLER_TERMINAL_OFFER_MISMATCH")

            cursor.execute(
                """SELECT cycle_id, role, counterpart_city_id, memo, status,
                          swap_id, give_asset, give_amount, take_asset
                   FROM swap_records WHERE idpk = %s FOR UPDATE""", (bid_idpk,),
            )
            swap = cursor.fetchone()
            cursor.execute(
                """SELECT offer_id, quantity, status FROM offer_reservations
                   WHERE bid_idpk = %s FOR UPDATE""", (bid_idpk,),
            )
            reservation = cursor.fetchone()

            if target == "REJECTED":
                # Rechazo previo a Wallet: no se creó swap ni reserva.
                _require(swap is None and reservation is None
                         and bid_status == "REJECTED",
                         "SELLER_TERMINAL_REJECTION_UNCONFIRMED")
                wallet_status = "REJECTED"
                swap_id = None
            else:
                _require(swap is not None and reservation is not None,
                         "SELLER_TERMINAL_ECONOMIC_ROWS_MISSING")
                (swap_cycle, role, counterpart, memo, wallet_status, swap_id,
                 give_asset, give_amount, take_asset) = swap
                _require(swap_cycle == cycle_id and role == "PROPOSER"
                         and counterpart == buyer and memo == bid_offer
                         and give_asset == "KWH" and take_asset == "CR"
                         and Decimal(give_amount) == Decimal(quantity),
                         "SELLER_TERMINAL_ECONOMIC_IDENTITY_MISMATCH")
                _require(reservation == (bid_offer, quantity,
                         "COMMITTED" if target == "SETTLED" else "RELEASED")
                         or (reservation is not None
                             and reservation[:2] == (bid_offer, quantity)
                             and reservation[2] == "HOLD"),
                         "SELLER_TERMINAL_RESERVATION_MISMATCH")
                if wallet_status not in TERMINAL_WALLET:
                    raise SellerTerminalPending("SELLER_TERMINAL_WALLET_NOT_TERMINAL")
                if target == "SETTLED":
                    _require(wallet_status == "SETTLED", "SELLER_TERMINAL_SETTLEMENT_MISMATCH")
                    _require(reservation[2] == "COMMITTED",
                             "SELLER_TERMINAL_SETTLEMENT_NOT_APPLIED")
                else:
                    _require(wallet_status in {"REJECTED", "EXPIRED", "CANCELLED"},
                             "SELLER_TERMINAL_COMPENSATION_MISMATCH")
                    # WalletService libera el HOLD en la transacción que persiste
                    # el evento. Si aún está retenido, NO fingir compensación.
                    _require(reservation[2] == "RELEASED",
                             "SELLER_TERMINAL_RELEASE_NOT_APPLIED")
                _require(isinstance(swap_id, str) and bool(swap_id),
                         "SELLER_TERMINAL_SWAP_ID_MISSING")
                _require(execution_swap is None or execution_swap == swap_id,
                         "SELLER_TERMINAL_SWAP_ID_CONFLICT")
                _require(bid_swap is None or bid_swap == swap_id,
                         "SELLER_TERMINAL_BID_SWAP_ID_CONFLICT")
                _require(bid_status in {"RESERVED", "ACCEPTED"},
                         "SELLER_TERMINAL_BID_STATUS_MISMATCH")

            if saga_status == target:
                return {"status": target, "alreadyFinalized": True, "swapId": swap_id,
                        "walletStatus": wallet_status}
            _require(saga_status in ACTIVE_SAGA, "SELLER_TERMINAL_ALREADY_DIFFERENT")
            cursor.execute(
                """UPDATE saga_executions
                   SET status = %s, swap_id = %s, updated_at = %s, terminal_at = %s
                   WHERE id = %s""",
                (target, swap_id, moment, moment, execution_pk),
            )
            _require(cursor.rowcount == 1, "SELLER_TERMINAL_UPDATE_FAILED")
            cursor.execute(
                """UPDATE saga_task_tokens SET status = 'SUPERSEDED', completed_at = %s
                   WHERE saga_execution_id = %s AND status = 'WAITING'""",
                (moment, execution_pk),
            )
        connection.commit()
    return {"status": target, "alreadyFinalized": False, "swapId": swap_id,
            "walletStatus": wallet_status}


def compensate_seller(
    store: Any, *, bid_idpk: str, offer_id: str, saga_execution_id: str,
) -> dict[str, Any]:
    """Cerrar compensada SÓLO después del RELEASE del evento Wallet."""
    return _complete_seller(store, bid_idpk=bid_idpk,
                            saga_execution_id=saga_execution_id,
                            offer_id=offer_id, target="COMPENSATED")


def finalize_seller(
    store: Any, *, saga_execution_id: str, status: str,
) -> dict[str, Any]:
    """Cerrar SETTLED o rechazo previo a swap, sin tocar el ledger."""
    _require(status in {"SETTLED", "REJECTED"}, "SELLER_TERMINAL_INVALID_FINAL_STATUS")
    return _complete_seller(store, bid_idpk=saga_execution_id,
                            saga_execution_id=saga_execution_id, target=status)


def known_seller_swap_id(store: Any, *, saga_execution_id: str,
                         idempotency_key: str) -> str:
    """Resolver id conocido para GET; jamás inventar consulta Wallet por key."""
    _identity(saga_execution_id, idempotency_key)
    with store.connect(store.dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """SELECT se.role, se.cycle_id, sr.cycle_id, sr.role,
                          sr.idempotency_key, sr.swap_id
                   FROM saga_executions AS se
                   JOIN swap_records AS sr ON sr.idpk = se.idpk
                   WHERE se.idpk = %s""",
                (saga_execution_id,),
            )
            row = cursor.fetchone()
    _require(row is not None and row[:2] == ("SELLER", row[2])
             and row[3] == "PROPOSER" and row[4] == idempotency_key,
             "SELLER_RECONCILIATION_IDENTITY_MISMATCH")
    if not isinstance(row[5], str) or not row[5]:
        raise SellerTerminalPending("SELLER_RECONCILIATION_SWAP_ID_NOT_KNOWN")
    return row[5]
