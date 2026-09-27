from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from sqlmodel import Session, select

from ..models import (
    LedgerEntry,
    Negotiation,
    OutboundMessage,
)

from .ledger import apply_ledger_effect

from .message_audit import (
    OUTBOUND_PENDING,
)

from .negotiation_rules import (
    round2,
)

from .negotiation_state import (
    NEGOTIATION_CONFIRMED,
    NEGOTIATION_PAID,
    transition_negotiation,
)


class NegotiationPaymentError(ValueError):
    pass


def _json_number(
    value: Decimal,
) -> int | float:
    """
    Convierte Decimal a número serializable en JSON.
    """

    if value == value.to_integral_value():
        return int(value)

    return float(value)


def _expected_payment(
    negotiation: Negotiation,
) -> Decimal:
    """
    Monto acordado:

        round2(
            confirmed_energy
            * confirmed_price
        )
    """

    if negotiation.confirmed_energy is None:
        raise NegotiationPaymentError(
            "negotiation has no confirmed energy"
        )

    if negotiation.confirmed_price is None:
        raise NegotiationPaymentError(
            "negotiation has no confirmed price"
        )

    return round2(
        negotiation.confirmed_energy
        * negotiation.confirmed_price
    )


# ============================================================
# Emitir transfer de pago para TAKE
# ============================================================

def enqueue_take_payment(
    session: Session,
    *,
    negotiation_id: int,
    city_id: str,
    routing_key: str,
    now: datetime | None = None,
) -> tuple[
    Negotiation,
    OutboundMessage,
    LedgerEntry,
    bool,
]:
    """
    Crea y persiste el transfer correspondiente a una
    negociación TAKE confirmada.

    También descuenta el pago del budget mediante el ledger.

    Retorna:
        (..., True)  -> pago creado
        (..., False) -> ya existía

    NO hace commit.
    """

    if now is None:
        now = datetime.now(timezone.utc)

    # --------------------------------------------------------
    # 1. Obtener y bloquear negociación
    # --------------------------------------------------------

    negotiation = session.exec(
        select(Negotiation)
        .where(
            Negotiation.id == negotiation_id
        )
        .with_for_update()
    ).first()

    if negotiation is None:
        raise NegotiationPaymentError(
            "negotiation not found"
        )

    # --------------------------------------------------------
    # 2. Solo TAKE genera un pago saliente
    # --------------------------------------------------------

    if negotiation.direction != "take":
        raise NegotiationPaymentError(
            "only take negotiations emit payment"
        )

    if negotiation.status not in {
        NEGOTIATION_CONFIRMED,
        NEGOTIATION_PAID,
    }:
        raise NegotiationPaymentError(
            "take negotiation is not confirmed"
        )

    # --------------------------------------------------------
    # 3. Evitar crear el pago dos veces
    # --------------------------------------------------------

    if negotiation.payment_quantity is not None:

        entry = session.exec(
            select(LedgerEntry).where(
                LedgerEntry.negotiation_id
                == negotiation.id,
                LedgerEntry.operation_type
                == "PAYMENT_SENT",
            )
        ).first()

        outbound = session.exec(
            select(OutboundMessage).where(
                OutboundMessage.msg_id
                == negotiation.latest_msg_id
            )
        ).first()

        if entry is None or outbound is None:
            raise NegotiationPaymentError(
                "inconsistent persisted take payment"
            )

        return (
            negotiation,
            outbound,
            entry,
            False,
        )

    # --------------------------------------------------------
    # 4. Calcular monto
    # --------------------------------------------------------

    quantity = _expected_payment(
        negotiation
    )

    # E1-46 dejó latest_msg_id apuntando
    # al msgId de la confirmación TAKE.
    confirmation_msg_id = (
        negotiation.latest_msg_id
    )

    if confirmation_msg_id is None:
        raise NegotiationPaymentError(
            "confirmation msgId is missing"
        )

    # --------------------------------------------------------
    # 5. Crear identidad del transfer
    # --------------------------------------------------------

    transfer_msg_id = str(
        uuid4()
    )

    transfer_idpk = str(
        uuid4()
    )

    # --------------------------------------------------------
    # 6. Construir mensaje E1
    # --------------------------------------------------------

    payload = {
        "idpk": transfer_idpk,
        "msgId": transfer_msg_id,
        "type": "transfer",
        "timestamp": (
            now.isoformat()
            .replace("+00:00", "Z")
        ),
        "cityId": city_id,
        "cycleId": negotiation.cycle_id,
        "data": {
            "becauseOf": confirmation_msg_id,
            "quantity": _json_number(
                quantity
            ),
        },
    }

    # --------------------------------------------------------
    # 7. Aplicar pago al ledger
    #
    # TAKE:
    # nosotros pagamos
    # => budget disminuye
    # --------------------------------------------------------

    ledger_entry, applied = apply_ledger_effect(
        session,

        cycle_id=negotiation.cycle_id,

        idpk=transfer_idpk,

        source_msg_id=transfer_msg_id,

        operation_type="PAYMENT_SENT",

        budget_delta=-quantity,

        energy_delta=Decimal("0"),

        details=payload,

        negotiation_id=negotiation.id,
    )

    if not applied:
        raise NegotiationPaymentError(
            "take payment ledger effect already exists"
        )

    # --------------------------------------------------------
    # 8. Crear OutboundMessage
    #
    # El connector existente será quien realmente
    # lo publique a RabbitMQ.
    # --------------------------------------------------------

    outbound = OutboundMessage(
        msg_id=transfer_msg_id,
        idpk=transfer_idpk,

        message_type="transfer",

        cycle_id=negotiation.cycle_id,

        payload=payload,

        # msgId de la confirmación TAKE.
        target_msg_id=confirmation_msg_id,

        routing_key=routing_key,

        status=OUTBOUND_PENDING,

        attempt_count=0,

        created_at=now,

        dispatch_required=True,
    )

    session.add(outbound)

    # --------------------------------------------------------
    # 9. Guardar pago en la negociación
    # --------------------------------------------------------

    negotiation.payment_quantity = quantity

    # Ahora latest_msg_id pasa a apuntar al transfer.
    # Esto permitirá que message_audit encuentre la
    # negociación cuando el connector informe PUBLISHED.
    negotiation.latest_msg_id = (
        transfer_msg_id
    )

    negotiation.updated_at = now

    session.add(negotiation)

    session.flush()

    return (
        negotiation,
        outbound,
        ledger_entry,
        True,
    )


# ============================================================
# Procesar transfer recibido por un GIVE
# ============================================================

def process_give_payment(
    session: Session,
    *,
    cycle_id: str,
    idpk: str,
    msg_id: str,
    because_of: str,
    quantity: Decimal,
    details: dict,
    now: datetime | None = None,
) -> tuple[
    Negotiation,
    LedgerEntry,
    bool,
]:
    """
    Procesa un transfer recibido desde la central como
    pago de una negociación GIVE.

    becauseOf debe ser el msgId de la confirmación GIVE.

    NO hace commit.
    """

    if now is None:
        now = datetime.now(timezone.utc)

    if quantity <= Decimal("0"):
        raise NegotiationPaymentError(
            "payment quantity must be positive"
        )

    # --------------------------------------------------------
    # 1. Encontrar la negociación mediante becauseOf
    # --------------------------------------------------------

    negotiation = session.exec(
        select(Negotiation)
        .where(
            Negotiation.latest_msg_id
            == because_of
        )
        .with_for_update()
    ).first()

    if negotiation is None:
        raise NegotiationPaymentError(
            "payment does not match "
            "a confirmed negotiation"
        )

    # --------------------------------------------------------
    # 2. Validar ciclo
    # --------------------------------------------------------

    if negotiation.cycle_id != cycle_id:
        raise NegotiationPaymentError(
            "payment cycle does not match negotiation"
        )

    # --------------------------------------------------------
    # 3. Solo GIVE puede recibir este pago
    # --------------------------------------------------------

    if negotiation.direction != "give":
        raise NegotiationPaymentError(
            "received negotiation payment "
            "must correspond to give"
        )

    # --------------------------------------------------------
    # 4. Debe estar CONFIRMED o ya PAID
    # --------------------------------------------------------

    if negotiation.status not in {
        NEGOTIATION_CONFIRMED,
        NEGOTIATION_PAID,
    }:
        raise NegotiationPaymentError(
            "give negotiation is not confirmed"
        )

    # --------------------------------------------------------
    # 5. Comprobar monto
    # --------------------------------------------------------

    expected_quantity = _expected_payment(
        negotiation
    )

    if quantity != expected_quantity:
        raise NegotiationPaymentError(
            f"invalid payment quantity: "
            f"expected {expected_quantity}, "
            f"received {quantity}"
        )

    # --------------------------------------------------------
    # 6. Protección extra:
    # no pagar dos veces la misma venta aunque venga
    # con un idpk distinto.
    # --------------------------------------------------------

    if negotiation.payment_quantity is not None:

        existing_entry = session.exec(
            select(LedgerEntry).where(
                LedgerEntry.negotiation_id
                == negotiation.id,
                LedgerEntry.operation_type
                == "PAYMENT_RECEIVED",
            )
        ).first()

        if existing_entry is None:
            raise NegotiationPaymentError(
                "inconsistent persisted give payment"
            )

        return (
            negotiation,
            existing_entry,
            False,
        )

    # --------------------------------------------------------
    # 7. Aplicar pago al ledger
    #
    # GIVE:
    # central nos paga
    # => budget aumenta
    # --------------------------------------------------------

    ledger_entry, applied = apply_ledger_effect(
        session,

        cycle_id=cycle_id,

        idpk=idpk,

        source_msg_id=msg_id,

        operation_type="PAYMENT_RECEIVED",

        budget_delta=quantity,

        energy_delta=Decimal("0"),

        details=details,

        negotiation_id=negotiation.id,
    )

    if not applied:
        return (
            negotiation,
            ledger_entry,
            False,
        )

    # --------------------------------------------------------
    # 8. Guardar pago
    # --------------------------------------------------------

    negotiation.payment_quantity = quantity

    # --------------------------------------------------------
    # 9. CONFIRMED -> PAID
    # --------------------------------------------------------

    transition_negotiation(
        session,
        negotiation,
        NEGOTIATION_PAID,
        now=now,
    )

    # OJO:
    # En GIVE dejamos latest_msg_id apuntando al msgId
    # de la confirmación GIVE.
    #
    # Esto permite detectar otro transfer que intente pagar
    # nuevamente el mismo becauseOf.
    #
    # El msgId del transfer igualmente queda registrado en:
    # LedgerEntry.source_msg_id e InboundMessage.

    session.add(negotiation)

    session.flush()

    return (
        negotiation,
        ledger_entry,
        True,
    )