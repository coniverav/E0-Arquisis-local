"""E3-041: confirmar el bid-accepted durable creado por el ingreso Wallet.

El evento oficial swap-proposed se procesa en master/routers/internal_messages,
que aplica los cambios y agrega bid-accepted al outbox en el mismo commit.
Esta Lambda NUNCA duplica esa escritura ni construye un JWT a partir de HTTP.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any


class SellerBidAcceptedPending(RuntimeError):
    """Aún no se confirmó durablemente swap-proposed + bid-accepted."""


class SellerBidAcceptedInconsistent(RuntimeError):
    """El mensaje persistido contradice la saga, la reserva o Wallet."""


def _require(ok: bool, reason: str) -> None:
    if not ok:
        raise SellerBidAcceptedInconsistent(reason)


def _utc(value: Any) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise SellerBidAcceptedInconsistent("SELLER_ACCEPTED_EXPIRES_AT_INVALID") from exc
    _require(isinstance(value, datetime) and value.tzinfo is not None
             and value.utcoffset() is not None, "SELLER_ACCEPTED_EXPIRES_AT_INVALID")
    return value.astimezone(timezone.utc)


def _decimal(value: Any) -> Decimal:
    _require(not isinstance(value, bool), "SELLER_ACCEPTED_AMOUNT_INVALID")
    try:
        result = Decimal(str(value))
    except (TypeError, ValueError, InvalidOperation) as exc:
        raise SellerBidAcceptedInconsistent("SELLER_ACCEPTED_AMOUNT_INVALID") from exc
    _require(result.is_finite(), "SELLER_ACCEPTED_AMOUNT_INVALID")
    return result


def verify_persisted_bid_accepted(
    store: Any, *, bid_idpk: str, cycle_id: str, swap_id: str,
    expires_at: str, saga_execution_id: str,
) -> dict[str, Any]:
    """Verifica SOLO lecturas; el outbox fue creado por el evento oficial Wallet.

    Una respuesta HTTP 202 o un swap PROPOSED sin evento/token no son evidencia.
    Retorna metadata no sensible, nunca el token JWT ni las credenciales.
    """
    for label, value in (
        ("BID_IDPK", bid_idpk), ("CYCLE_ID", cycle_id),
        ("SWAP_ID", swap_id), ("SAGA_IDPK", saga_execution_id),
    ):
        _require(isinstance(value, str) and bool(value),
                 f"SELLER_ACCEPTED_{label}_REQUIRED")
    _require(saga_execution_id == bid_idpk,
             "SELLER_ACCEPTED_SAGA_IDPK_MISMATCH")
    expected_exp = _utc(expires_at)
    now = _utc(store.now())

    with store.connect(store.dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """SELECT role, cycle_id, status FROM saga_executions
                   WHERE idpk = %s""", (bid_idpk,),
            )
            execution = cursor.fetchone()
            _require(execution is not None and execution[0] == "SELLER"
                     and execution[1] == cycle_id
                     and execution[2] in {"PENDING_START", "RUNNING", "WAITING"},
                     "SELLER_ACCEPTED_EXECUTION_MISMATCH")

            cursor.execute(
                """SELECT msg_id, cycle_id, offer_id, counterpart_city_id,
                          quantity, status, swap_id, accepted_price_per_energy
                   FROM market_bids WHERE idpk = %s AND direction = 'INCOMING'""",
                (bid_idpk,),
            )
            bid = cursor.fetchone()
            _require(bid is not None and bid[1] == cycle_id,
                     "SELLER_ACCEPTED_BID_UNKNOWN")
            bid_msg, _, offer_id, buyer, quantity, bid_status, bid_swap_id, price = bid
            if bid_status == "RESERVED":
                raise SellerBidAcceptedPending("SELLER_ACCEPTED_WAITING_FOR_WALLET_EVENT")
            _require(bid_status == "ACCEPTED" and bid_swap_id == swap_id,
                     "SELLER_ACCEPTED_BID_STATE_MISMATCH")

            cursor.execute(
                """SELECT cycle_id, role, counterpart_city_id, give_asset,
                          give_amount, take_asset, take_amount, memo,
                          status, swap_id, swap_token, expires_at, idempotency_key
                   FROM swap_records WHERE idpk = %s""", (bid_idpk,),
            )
            record = cursor.fetchone()
            _require(record is not None, "SELLER_ACCEPTED_SWAP_UNKNOWN")
            (swap_cycle, role, counterpart, give_asset, give_amount, take_asset,
             take_amount, memo, swap_status, persisted_swap_id, token,
             record_exp, idempotency_key) = record
            _require(swap_cycle == cycle_id and role == "PROPOSER"
                     and counterpart == buyer and give_asset == "KWH"
                     and take_asset == "CR" and memo == offer_id
                     and idempotency_key == bid_idpk
                     and swap_status in {"PROPOSED", "SETTLED"}
                     and persisted_swap_id == swap_id
                     and isinstance(token, str) and bool(token)
                     and len(token) <= 16384 and record_exp is not None,
                     "SELLER_ACCEPTED_SWAP_STATE_MISMATCH")
            _require(_decimal(give_amount) == _decimal(quantity)
                     and _decimal(take_amount) == (
                         _decimal(price) * _decimal(quantity)
                     ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
                     "SELLER_ACCEPTED_ECONOMIC_MISMATCH")
            _require(abs((_utc(record_exp) - expected_exp).total_seconds()) <= 1,
                     "SELLER_ACCEPTED_EXPIRY_MISMATCH")

            cursor.execute(
                """SELECT offer_id, quantity, status FROM offer_reservations
                   WHERE bid_idpk = %s""", (bid_idpk,),
            )
            reservation = cursor.fetchone()
            _require(reservation is not None and reservation[:2] == (offer_id, quantity)
                     and reservation[2] in {"HOLD", "COMMITTED"},
                     "SELLER_ACCEPTED_RESERVATION_MISMATCH")

            cursor.execute(
                """SELECT msg_id, idpk, payload, routing_key, exchange_name,
                          status, dispatch_required, cycle_id, target_msg_id,
                          expires_at, delivery_kind
                   FROM outbound_messages
                   WHERE message_type = 'bid-accepted' AND target_msg_id = %s
                   ORDER BY id""", (bid_msg,),
            )
            rows = cursor.fetchall()

    if not rows:
        raise SellerBidAcceptedPending("SELLER_ACCEPTED_OUTBOX_NOT_COMMITTED")
    _require(len(rows) == 1, "SELLER_ACCEPTED_DUPLICATE_OUTBOX")
    (message_id, outbox_idpk, envelope, route, exchange, status,
     dispatch_required, outbox_cycle, target_msg, outbox_exp, delivery_kind) = rows[0]
    _require(isinstance(envelope, dict) and isinstance(envelope.get("data"), dict),
             "SELLER_ACCEPTED_OUTBOX_PAYLOAD_INVALID")
    data = envelope["data"]
    _require(envelope.get("type") == "bid-accepted"
             and envelope.get("msgId") == message_id
             and envelope.get("idpk") == outbox_idpk
             and envelope.get("cityId") == store.city_id
             and envelope.get("cycleId") == cycle_id
             and outbox_cycle == cycle_id and target_msg == bid_msg
             and route == f"city.{buyer}" and exchange == "energy.x"
             and delivery_kind == "BROKER"
             and status in {"PENDING", "CLAIMED", "FAILED", "PUBLISHED"}
             and (status == "PUBLISHED" or dispatch_required is True),
             "SELLER_ACCEPTED_OUTBOX_METADATA_MISMATCH")
    _require(data.get("target") == bid_msg and data.get("offerId") == offer_id
             and type(data.get("quantity")) is int and data["quantity"] == quantity
             and data.get("swapId") == swap_id
             and data.get("swapToken") == token
             and _decimal(data.get("amount")) == _decimal(take_amount)
             and _decimal(data.get("pricePerEnergy")) == _decimal(price),
             "SELLER_ACCEPTED_OUTBOX_CONTENT_MISMATCH")
    _require(outbox_exp is not None
             and abs((_utc(outbox_exp) - expected_exp).total_seconds()) <= 1
             and abs((_utc(data.get("expiresAt")) - expected_exp).total_seconds()) <= 1,
             "SELLER_ACCEPTED_OUTBOX_EXPIRY_MISMATCH")
    if expected_exp <= now:
        raise SellerBidAcceptedPending("SELLER_ACCEPTED_TOKEN_EXPIRED")
    return {
        "status": "PUBLISHED" if status == "PUBLISHED" else "QUEUED",
        "alreadyEnqueued": True,
        "outboxMsgId": message_id,
        "swapId": swap_id,
    }
