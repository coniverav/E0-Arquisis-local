"""E3-040: petición Wallet seller idempotente, con cuerpo durable.

La reserva AD4 ya existe al iniciar la saga. El task token se persiste antes
que esta preparación y el POST. Nunca se reconstruye el cuerpo usando la
oferta actual ni se repite un POST con expiresAt regenerado.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping


class SellerProposalInconsistent(RuntimeError):
    """El input de la saga no coincide con la intención persistida."""


class SellerProposalExpired(RuntimeError):
    """No enviar una propuesta Wallet nueva con vencimiento caducado."""


def _require(ok: bool, detail: str) -> None:
    if not ok:
        raise SellerProposalInconsistent(detail)


def _decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, bool):
        raise SellerProposalInconsistent(f"SELLER_PROPOSAL_{field}_INVALID")
    try:
        result = Decimal(str(value))
    except (TypeError, ValueError, InvalidOperation) as exc:
        raise SellerProposalInconsistent(f"SELLER_PROPOSAL_{field}_INVALID") from exc
    _require(result.is_finite(), f"SELLER_PROPOSAL_{field}_INVALID")
    return result


def _instant(value: datetime, field: str) -> datetime:
    _require(
        isinstance(value, datetime) and value.tzinfo is not None
        and value.utcoffset() is not None,
        f"SELLER_PROPOSAL_{field}_TIMEZONE_REQUIRED",
    )
    return value.astimezone(timezone.utc)


def prepare_seller_proposal(
    store: Any, *, saga_execution_id: str, idempotency_key: str,
    swap: Mapping[str, Any],
) -> dict[str, Any]:
    """Congela expiresAt bajo lock y entrega *solo* los 4 campos Wallet v3.

    Se confirma la transacción antes del HTTP; los reintentos usan la misma
    Idempotency-Key y exactamente el mismo cuerpo, incluso bajo concurrencia.
    """
    _require(isinstance(saga_execution_id, str) and bool(saga_execution_id),
             "SELLER_PROPOSAL_IDPK_REQUIRED")
    _require(isinstance(idempotency_key, str) and idempotency_key == saga_execution_id,
             "SELLER_PROPOSAL_IDEMPOTENCY_KEY_MISMATCH")
    _require(isinstance(swap, Mapping), "SELLER_PROPOSAL_SWAP_REQUIRED")
    _require(swap.get("idpk") == saga_execution_id
             and swap.get("idempotencyKey") == idempotency_key,
             "SELLER_PROPOSAL_INPUT_IDENTITY_MISMATCH")
    input_give = swap.get("give")
    input_take = swap.get("take")
    _require(isinstance(input_give, Mapping) and isinstance(input_take, Mapping),
             "SELLER_PROPOSAL_LEGS_REQUIRED")
    _require(input_give.get("asset") == "KWH" and input_take.get("asset") == "CR",
             "SELLER_PROPOSAL_ASSET_MISMATCH")
    input_kwh = _decimal(input_give.get("amount"), "GIVE")
    input_cr = _decimal(input_take.get("amount"), "TAKE")
    _require(input_kwh > 0 and input_kwh == input_kwh.to_integral_value()
             and input_cr > 0 and input_cr == input_cr.quantize(Decimal("0.01")),
             "SELLER_PROPOSAL_AMOUNTS_INVALID")
    now = _instant(store.now(), "NOW")

    with store.connect(store.dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """SELECT role, cycle_id, status
                   FROM saga_executions WHERE idpk = %s FOR UPDATE""",
                (saga_execution_id,),
            )
            execution = cursor.fetchone()
            _require(execution is not None and execution[0] == "SELLER"
                     and execution[2] in {"PENDING_START", "RUNNING", "WAITING"},
                     "SELLER_PROPOSAL_EXECUTION_MISMATCH")
            cycle_id = execution[1]

            cursor.execute(
                """SELECT cycle_id, role, counterpart_city_id, give_asset,
                          give_amount, take_asset, take_amount, memo, status,
                          idempotency_key, proposal_expires_at
                   FROM swap_records WHERE idpk = %s FOR UPDATE""",
                (saga_execution_id,),
            )
            record = cursor.fetchone()
            _require(record is not None, "SELLER_PROPOSAL_SWAP_NOT_FOUND")
            (swap_cycle, role, buyer, give_asset, give_amount,
             take_asset, take_amount, memo, status, key, expires_at) = record
            _require(swap_cycle == cycle_id and role == "PROPOSER"
                     and status in {"SUBMITTED", "PROPOSED"}
                     and key == idempotency_key
                     and give_asset == "KWH" and take_asset == "CR",
                     "SELLER_PROPOSAL_SWAP_MISMATCH")
            _require(give_amount == input_kwh and take_amount == input_cr
                     and memo == swap.get("memo")
                     and isinstance(memo, str) and len(memo.encode("utf-8")) <= 128,
                     "SELLER_PROPOSAL_IMMUTABLE_BODY_MISMATCH")

            cursor.execute(
                """SELECT cycle_id, offer_id, counterpart_city_id, quantity,
                          status, direction
                   FROM market_bids WHERE idpk = %s""",
                (saga_execution_id,),
            )
            bid = cursor.fetchone()
            _require(bid == (cycle_id, memo, buyer, int(input_kwh),
                             "RESERVED", "INCOMING")
                     or (bid is not None and bid[:4] == (
                         cycle_id, memo, buyer, int(input_kwh))
                         and bid[4] == "ACCEPTED" and bid[5] == "INCOMING"),
                     "SELLER_PROPOSAL_BID_MISMATCH")

            cursor.execute(
                """SELECT offer_id, quantity, status
                   FROM offer_reservations WHERE bid_idpk = %s""",
                (saga_execution_id,),
            )
            hold = cursor.fetchone()
            _require(hold == (memo, int(input_kwh), "HOLD"),
                     "SELLER_PROPOSAL_HOLD_MISSING")

            cursor.execute(
                "SELECT market_closes_at FROM cycles WHERE cycle_id = %s",
                (cycle_id,),
            )
            cycle = cursor.fetchone()
            _require(cycle is not None and cycle[0] is not None,
                     "SELLER_PROPOSAL_MARKET_CLOSE_MISSING")
            closes_at = _instant(cycle[0], "MARKET_CLOSE")

            if expires_at is None:
                # Wallet solo acepta expiraciones a <=60 s desde el POST.
                # 55 s deja margen a latencia, y el cierre es cota superior.
                proposed = min(now + timedelta(seconds=55), closes_at)
                if proposed <= now + timedelta(seconds=5):
                    raise SellerProposalExpired("SELLER_PROPOSAL_WINDOW_CLOSED")
                cursor.execute(
                    """UPDATE swap_records
                       SET proposal_expires_at = %s
                       WHERE idpk = %s AND proposal_expires_at IS NULL""",
                    (proposed, saga_execution_id),
                )
                _require(cursor.rowcount == 1,
                         "SELLER_PROPOSAL_FREEZE_RACE")
                expires_at = proposed
            else:
                expires_at = _instant(expires_at, "EXPIRES_AT")
                if expires_at <= now + timedelta(seconds=3):
                    raise SellerProposalExpired("SELLER_PROPOSAL_EXPIRES_AT_PASSED")

        connection.commit()

    return {
        "give": {"asset": "KWH", "amount": int(input_kwh)},
        "take": {"asset": "CR", "amount": float(input_cr)},
        "expiresAt": expires_at.isoformat().replace("+00:00", "Z"),
        "memo": memo,
    }
