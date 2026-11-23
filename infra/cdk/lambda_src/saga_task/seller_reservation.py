"""E3-039: adaptación sin escrituras de la reserva y rechazo seller de AD4.

La decisión comercial ya se confirma en Master *antes* de StartExecution.
Esta Lambda verifica esa decisión durable: nunca crea otro HOLD, swap,
rechazo u outbox, ni consulta Wallet. Un estado inconsistente se detiene.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any


class SellerInsufficientEnergy(RuntimeError):
    """Bid previamente rechazado por AD4 debido a falta de KWH."""


class SellerReservationInconsistent(RuntimeError):
    """La ejecución no coincide con los registros durables de AD4/AD8."""


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise SellerReservationInconsistent(reason)


def _required_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} debe ser un string no vacío")
    return value


def _rejected_outbox(cursor: Any, bid_idpk: str) -> dict[str, Any]:
    """Lee el único rechazo construido por Master para este bid."""
    cursor.execute(
        """SELECT b.msg_id, b.cycle_id, b.offer_id, b.quantity,
                  b.counterpart_city_id, b.status, b.rejection_reason,
                  b.available_on_rejection
           FROM market_bids b
           WHERE b.idpk = %s AND b.direction = 'INCOMING'
           FOR UPDATE""",
        (bid_idpk,),
    )
    bid = cursor.fetchone()
    _require(bid is not None, "SELLER_BID_NOT_FOUND")
    msg_id, cycle_id, offer_id, quantity, buyer, status, reason, available = bid
    _require(status == "REJECTED", "SELLER_BID_NOT_REJECTED")
    _require(reason in {"QUANTITY_UNAVAILABLE", "BID_TOO_SMALL", "OFFER_WITHDRAWN",
                        "MARKET_CLOSED", "SELF_TRADE"}, "SELLER_REJECTION_REASON_INVALID")
    cursor.execute(
        """SELECT payload, routing_key, cycle_id
           FROM outbound_messages
           WHERE message_type = 'bid-rejected' AND target_msg_id = %s""",
        (msg_id,),
    )
    responses = cursor.fetchall()
    _require(len(responses) == 1, "SELLER_REJECTION_OUTBOX_NOT_UNIQUE")
    payload, routing_key, outbound_cycle = responses[0]
    data = payload.get("data") if isinstance(payload, dict) else None
    _require(
        isinstance(data, dict)
        and payload.get("reason") == reason
        and data.get("target") == msg_id
        and data.get("offerId") == offer_id
        and data.get("requested") == quantity
        and routing_key == f"city.{buyer}"
        and outbound_cycle == cycle_id,
        "SELLER_REJECTION_OUTBOX_MISMATCH",
    )
    if reason == "QUANTITY_UNAVAILABLE":
        _require(
            isinstance(available, int) and available >= 0
            and data.get("available") == available and quantity > available,
            "SELLER_REJECTION_AVAILABLE_MISMATCH",
        )
    else:
        _require("available" not in data, "SELLER_REJECTION_UNEXPECTED_AVAILABLE")
    cursor.execute(
        "SELECT 1 FROM offer_reservations WHERE bid_idpk = %s", (bid_idpk,)
    )
    _require(cursor.fetchone() is None, "SELLER_REJECTED_BID_HAS_HOLD")
    cursor.execute(
        "SELECT 1 FROM swap_records WHERE idpk = %s", (bid_idpk,)
    )
    _require(cursor.fetchone() is None, "SELLER_REJECTED_BID_HAS_SWAP")
    cursor.execute(
        """SELECT 1 FROM outbound_messages WHERE idpk = %s
           AND message_type = 'SAGA_START' AND delivery_kind = 'STEP_FUNCTIONS'""",
        (bid_idpk,),
    )
    _require(cursor.fetchone() is None, "SELLER_REJECTED_BID_HAS_SAGA_START")
    return {"status": "REJECTED", "reason": reason, "available": available,
            "bidIdpk": bid_idpk, "outboxVerified": True}


def verify_seller_rejection(store: Any, *, bid_idpk: str, reason: str) -> dict[str, Any]:
    """No publica otro mensaje: verifica la única respuesta durable existente."""
    _required_str(bid_idpk, "bidIdpk")
    _required_str(reason, "reason")
    with store.connect(store.dsn) as connection:
        with connection.cursor() as cursor:
            result = _rejected_outbox(cursor, bid_idpk)
            _require(result["reason"] == reason, "SELLER_REJECTION_REASON_MISMATCH")
            return result


def verify_seller_reservation(
    store: Any, *, bid_idpk: str, cycle_id: str, offer_id: str,
    quantity: int, saga_execution_id: str,
) -> dict[str, Any]:
    """Valida una reserva AD4 que ya fue creada en el commit del ingress.

    Orden de lectura bloqueada: oferta -> bid. La comprobación es idempotente
    incluso tras un retry Lambda, y jamás descuenta KWH de nuevo.
    """
    for name, value in (("bidIdpk", bid_idpk), ("cycleId", cycle_id),
                        ("offerId", offer_id), ("sagaExecutionId", saga_execution_id)):
        _required_str(value, name)
    if type(quantity) is not int or quantity <= 0:
        raise ValueError("quantity debe ser entero positivo")
    _require(saga_execution_id == bid_idpk, "SELLER_EXECUTION_IDPK_MISMATCH")

    with store.connect(store.dsn) as connection:
        with connection.cursor() as cursor:
            # Localizar sin bloquear el bid para respetar el orden AD4.
            cursor.execute(
                "SELECT offer_id FROM market_bids WHERE idpk = %s AND direction = 'INCOMING'",
                (bid_idpk,),
            )
            preliminary = cursor.fetchone()
            _require(preliminary is not None and preliminary[0] == offer_id,
                     "SELLER_BID_OFFER_MISMATCH")
            cursor.execute(
                """SELECT cycle_id, city_id, is_local, remaining_quantity
                   FROM market_offers WHERE offer_id = %s FOR UPDATE""",
                (offer_id,),
            )
            offer = cursor.fetchone()
            _require(offer is not None, "SELLER_OFFER_NOT_FOUND")
            _require(offer[0] == cycle_id and offer[1] == store.city_id and offer[2],
                     "SELLER_OFFER_IDENTITY_MISMATCH")
            remaining = offer[3]
            cursor.execute(
                """SELECT cycle_id, offer_id, quantity, status, counterpart_city_id,
                          rejection_reason, accepted_price_per_energy
                   FROM market_bids
                   WHERE idpk = %s AND direction = 'INCOMING' FOR UPDATE""",
                (bid_idpk,),
            )
            bid = cursor.fetchone()
            _require(bid is not None, "SELLER_BID_NOT_FOUND")
            _require(bid[:3] == (cycle_id, offer_id, quantity),
                     "SELLER_BID_INPUT_MISMATCH")
            bid_status = bid[3]
            if bid_status == "REJECTED" and bid[5] == "QUANTITY_UNAVAILABLE":
                # La ruta normal no inicia una saga rechazada. Si llega una
                # ejecución tardía/impropia, exigir rechazo ya persistido.
                _rejected_outbox(cursor, bid_idpk)
                raise SellerInsufficientEnergy("SELLER_BID_ALREADY_REJECTED")
            _require(bid_status == "RESERVED", "SELLER_BID_NOT_RESERVED")

            cursor.execute(
                "SELECT status_payload FROM cycles WHERE cycle_id = %s", (cycle_id,)
            )
            cycle = cursor.fetchone()
            _require(cycle is not None, "SELLER_CYCLE_NOT_FOUND")
            market = (cycle[0] or {}).get("market")
            min_bid = market.get("minBid") if isinstance(market, dict) else None
            _require(type(min_bid) is int and min_bid > 0 and quantity >= min_bid,
                     "SELLER_MIN_BID_MISMATCH")

            cursor.execute(
                """SELECT offer_id, quantity, status FROM offer_reservations
                   WHERE bid_idpk = %s""",
                (bid_idpk,),
            )
            hold = cursor.fetchone()
            _require(hold == (offer_id, quantity, "HOLD"),
                     "SELLER_HOLD_MISSING_OR_INCONSISTENT")
            cursor.execute(
                """SELECT cycle_id, role, counterpart_city_id, give_asset,
                          give_amount, take_asset, take_amount, memo, status,
                          idempotency_key
                   FROM swap_records WHERE idpk = %s""",
                (bid_idpk,),
            )
            swap = cursor.fetchone()
            _require(swap is not None and swap[0] == cycle_id and swap[1] == "PROPOSER"
                     and swap[2] == bid[4] and swap[3] == "KWH"
                     and Decimal(swap[4]) == Decimal(quantity)
                     and swap[5] == "CR" and swap[7] == offer_id
                     and swap[8] == "SUBMITTED" and swap[9] == bid_idpk,
                     "SELLER_SWAP_MISMATCH")
            _require(bid[6] is not None and Decimal(swap[6]) ==
                     (Decimal(quantity) * Decimal(bid[6])).quantize(
                         Decimal("0.01"), rounding=ROUND_HALF_UP),
                     "SELLER_SWAP_PRICE_MISMATCH")

            cursor.execute(
                """SELECT role, cycle_id, status FROM saga_executions
                   WHERE idpk = %s""",
                (bid_idpk,),
            )
            saga = cursor.fetchone()
            _require(saga is not None and saga[0] == "SELLER" and saga[1] == cycle_id
                     and saga[2] not in {"SETTLED", "REJECTED", "COMPENSATED", "TIMED_OUT", "FAILED"},
                     "SELLER_SAGA_IDENTITY_MISMATCH")
            cursor.execute(
                """SELECT execution_name, cycle_id FROM outbound_messages
                   WHERE idpk = %s AND delivery_kind = 'STEP_FUNCTIONS'
                     AND message_type = 'SAGA_START'""",
                (bid_idpk,),
            )
            intents = cursor.fetchall()
            _require(len(intents) == 1 and intents[0] == (bid_idpk, cycle_id),
                     "SELLER_START_INTENT_MISMATCH")
            cursor.execute(
                """SELECT COALESCE(SUM(quantity), 0) FROM offer_reservations
                   WHERE offer_id = %s AND status = 'HOLD'""",
                (offer_id,),
            )
            held = cursor.fetchone()[0]
            _require(held >= quantity and held <= remaining,
                     "SELLER_OVERBOOKED_OR_RELEASED")
            return {"status": "RESERVED", "bidIdpk": bid_idpk,
                    "quantity": quantity, "alreadyReserved": True,
                    "availableAfter": remaining - held}
