"""E3-046: aceptación Wallet del comprador con intento durable e identidad cerrada.

El token ya fue verificado por E3-045 en el estado anterior de la saga.
Aquí se confirma otra vez la correlación con PostgreSQL ANTES de tocar Wallet,
se persiste la intención de aceptar y se conserva la misma Idempotency-Key.
Nunca modifica el ledger ni interpreta HTTP 202 como liquidación. El evento
Wallet y la finalización/callback serán correlacionados en E3-047.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from errors import WalletRetryable


class BuyerAcceptanceInconsistent(ValueError):
    """Identidad económica incompatible; no ejecutar HTTP."""


class BuyerAcceptanceDeadline(RuntimeError):
    """La ventana de aceptación venció; se requiere conciliación."""


TERMINAL = frozenset({"SETTLED", "REJECTED", "EXPIRED", "CANCELLED"})
SAGA_ACTIVE = frozenset({"PENDING_START", "RUNNING", "WAITING"})


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise BuyerAcceptanceInconsistent(code)


def _utc(value: datetime, code: str) -> datetime:
    _require(isinstance(value, datetime) and value.tzinfo is not None
             and value.utcoffset() is not None, code)
    return value.astimezone(timezone.utc)


def _identity(saga_execution_id: Any, swap_record_id: Any,
              swap_id: Any, idempotency_key: Any) -> None:
    _require(isinstance(saga_execution_id, str) and bool(saga_execution_id),
             "BUYER_ACCEPTANCE_SAGA_REQUIRED")
    _require(type(swap_record_id) is int and swap_record_id > 0,
             "BUYER_ACCEPTANCE_RECORD_REQUIRED")
    _require(isinstance(swap_id, str) and bool(swap_id),
             "BUYER_ACCEPTANCE_SWAP_ID_REQUIRED")
    _require(isinstance(idempotency_key, str) and idempotency_key == saga_execution_id,
             "BUYER_ACCEPTANCE_KEY_MISMATCH")


def _row(cursor: Any, *, saga_execution_id: str, swap_record_id: int,
         locked: bool) -> tuple[Any, ...] | None:
    cursor.execute(
        """SELECT se.role, se.status, se.cycle_id, se.swap_id,
                  sr.idpk, sr.role, sr.swap_id, sr.cycle_id,
                  sr.status, sr.accept_idempotency_key,
                  sr.source_bid_idpk, sr.expires_at,
                  sr.give_asset, sr.give_amount, sr.take_asset, sr.take_amount,
                  sr.swap_token, sr.accept_attempts, sr.accept_error,
                  sr.accept_retry_at,
                  b.direction, b.status, b.cycle_id, b.swap_id,
                  b.counterpart_city_id, sr.counterpart_city_id, b.quantity,
                  c.market_closes_at
           FROM swap_records sr
           JOIN saga_executions se ON se.idpk = sr.idpk
           JOIN market_bids b ON b.idpk = sr.source_bid_idpk
           JOIN cycles c ON c.cycle_id = sr.cycle_id
           WHERE sr.id = %s AND se.idpk = %s"""
        + (" FOR UPDATE OF sr" if locked else ""),
        (swap_record_id, saga_execution_id),
    )
    return cursor.fetchone()


def _validate(row: tuple[Any, ...] | None, *, saga_execution_id: str,
              swap_id: str, key: str) -> tuple[str, int, datetime, datetime, Any, datetime | None]:
    _require(row is not None, "BUYER_ACCEPTANCE_RECORD_UNKNOWN")
    (saga_role, saga_status, saga_cycle, saga_swap,
     record_idpk, role, record_swap, record_cycle, record_status,
     accept_key, source_bid, expires_at,
     give_asset, give_amount, take_asset, take_amount,
     token, attempts, accept_error, accept_retry_at,
     bid_direction, bid_status, bid_cycle, bid_swap,
     bid_seller, seller, quantity, closes_at) = row
    _require(
        saga_role == "BUYER" and saga_cycle == record_cycle == bid_cycle
        and saga_swap == record_swap == bid_swap == swap_id
        and record_idpk == accept_key == key == saga_execution_id
        and role == "ACCEPTOR" and bool(source_bid)
        and bid_direction == "OUTGOING" and bid_status == "ACCEPTED"
        and bid_seller == seller
        and give_asset == "CR" and take_asset == "KWH"
        and Decimal(str(take_amount)) == Decimal(str(quantity))
        and Decimal(str(give_amount)).is_finite()
        and Decimal(str(give_amount)) > 0
        and isinstance(token, str) and bool(token)
        and isinstance(attempts, int) and attempts >= 0
        and record_status in (TERMINAL | {"SUBMITTED", "ACCEPT_SUBMITTED", "PROPOSED"})
        and (saga_status in SAGA_ACTIVE or record_status in TERMINAL),
        "BUYER_ACCEPTANCE_ECONOMIC_IDENTITY_MISMATCH",
    )
    _require(expires_at is not None and closes_at is not None,
             "BUYER_ACCEPTANCE_EXPIRY_REQUIRED")
    return (record_status, attempts,
            _utc(expires_at, "BUYER_ACCEPTANCE_EXPIRY_INVALID"),
            _utc(closes_at, "BUYER_ACCEPTANCE_CLOSE_INVALID"),
            accept_error, accept_retry_at)


def verify_buyer_acceptance_identity(
    store: Any, *, saga_execution_id: str, swap_record_id: int,
    swap_id: str, idempotency_key: str,
) -> dict[str, Any]:
    """Validación preliminar: no reemplaza un token de callback legítimo."""
    _identity(saga_execution_id, swap_record_id, swap_id, idempotency_key)
    with store.connect(store.dsn) as connection:
        with connection.cursor() as cursor:
            row = _row(cursor, saga_execution_id=saga_execution_id,
                       swap_record_id=swap_record_id, locked=False)
    status, attempts, expiry, close, error, retry_at = _validate(
        row, saga_execution_id=saga_execution_id, swap_id=swap_id,
        key=idempotency_key,
    )
    return {"status": status, "attempts": attempts}


def prepare_buyer_acceptance(
    store: Any, *, saga_execution_id: str, swap_record_id: int,
    swap_id: str, idempotency_key: str,
) -> dict[str, Any]:
    """Persiste la intención antes de HTTP, bajo lock del SwapRecord.

    POST sólo cuando no existe un intento exitosamente recibido ni hay otro
    intento en vuelo. Los errores reintentables conservan la misma llave.
    """
    _identity(saga_execution_id, swap_record_id, swap_id, idempotency_key)
    now = _utc(store.now(), "BUYER_ACCEPTANCE_NOW_INVALID")
    with store.connect(store.dsn) as connection:
        with connection.cursor() as cursor:
            row = _row(cursor, saga_execution_id=saga_execution_id,
                       swap_record_id=swap_record_id, locked=True)
            status, attempts, expiry, close, error, retry_at = _validate(
                row, saga_execution_id=saga_execution_id,
                swap_id=swap_id, key=idempotency_key,
            )
            if status in TERMINAL:
                return {"action": "AWAIT_EVENT", "reason": "TERMINAL_ALREADY_PERSISTED"}
            if status == "ACCEPT_SUBMITTED" and error in (None, "WALLET_TERMINAL_PENDING_EVENT"):
                return {"action": "AWAIT_EVENT", "reason": "HTTP_ALREADY_ACCEPTED"}
            if retry_at is not None and _utc(retry_at, "BUYER_ACCEPTANCE_RETRY_INVALID") > now:
                raise WalletRetryable("Aceptación Wallet en vuelo o en backoff")
            # La verificación criptográfica se ejecutó en el paso E3-045,
            # pero los plazos deben verificarse otra vez justo antes del POST.
            if min(expiry, close) <= now + timedelta(seconds=3):
                raise BuyerAcceptanceDeadline("BUYER_ACCEPTANCE_DEADLINE_EXPIRED")
            cursor.execute(
                """UPDATE swap_records
                   SET status = 'ACCEPT_SUBMITTED',
                       accept_attempts = accept_attempts + 1,
                       accept_attempted_at = %s,
                       accept_error = 'WALLET_IN_FLIGHT',
                       accept_retry_at = %s
                   WHERE id = %s AND status IN ('SUBMITTED', 'ACCEPT_SUBMITTED', 'PROPOSED')""",
                (now, now + timedelta(seconds=10), swap_record_id),
            )
            _require(cursor.rowcount == 1, "BUYER_ACCEPTANCE_UPDATE_RACE")
        connection.commit()
    return {"action": "POST", "attempt": attempts + 1}


def complete_buyer_acceptance_attempt(
    store: Any, *, saga_execution_id: str, swap_record_id: int,
    swap_id: str, idempotency_key: str,
    status_code: int | None = None, wallet_status: str | None = None,
    failure: str | None = None,
) -> dict[str, Any]:
    """Registra resultado HTTP sin pisar un evento Wallet terminal concurrente.

    202 y 200 no son asientos contables: el ledger y task callback pertenecen
    al evento del consumidor EC2 y su conciliación (E3-047).
    """
    _identity(saga_execution_id, swap_record_id, swap_id, idempotency_key)
    _require((failure is None) != (status_code is None),
             "BUYER_ACCEPTANCE_RESULT_INVALID")
    if status_code is not None:
        _require(status_code in {200, 202}, "BUYER_ACCEPTANCE_HTTP_INVALID")
    if failure is not None:
        _require(isinstance(failure, str) and bool(failure),
                 "BUYER_ACCEPTANCE_FAILURE_INVALID")
    with store.connect(store.dsn) as connection:
        with connection.cursor() as cursor:
            row = _row(cursor, saga_execution_id=saga_execution_id,
                       swap_record_id=swap_record_id, locked=True)
            status, attempts, expiry, close, error, retry_at = _validate(
                row, saga_execution_id=saga_execution_id, swap_id=swap_id,
                key=idempotency_key,
            )
            if status in TERMINAL:
                return {"status": status, "eventWonRace": True}
            if status != "ACCEPT_SUBMITTED" or attempts <= 0:
                raise BuyerAcceptanceInconsistent("BUYER_ACCEPTANCE_NO_PENDING_ATTEMPT")
            # Si la respuesta vino de un HTTP 200 terminal conocido, tampoco
            # inferir SETTLED hasta que el evento/autorización económica se
            # haya aplicado en PostgreSQL.
            if failure is None:
                new_error = ("WALLET_TERMINAL_PENDING_EVENT"
                             if status_code == 200 and
                             str(wallet_status or "").upper() in TERMINAL else None)
                next_retry_at = None
            else:
                new_error = failure[:100]
                next_retry_at = _utc(store.now(), "BUYER_ACCEPTANCE_NOW_INVALID") + timedelta(seconds=2)
            cursor.execute(
                """UPDATE swap_records
                   SET accept_error = %s, accept_retry_at = %s
                   WHERE id = %s AND status = 'ACCEPT_SUBMITTED'""",
                (new_error, next_retry_at, swap_record_id),
            )
            _require(cursor.rowcount == 1, "BUYER_ACCEPTANCE_RESULT_RACE")
        connection.commit()
    return {"status": "ACCEPT_SUBMITTED", "eventWonRace": False}
