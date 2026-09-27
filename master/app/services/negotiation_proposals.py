from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from sqlmodel import Session

from ..models import (
    Cycle,
    Negotiation,
    OutboundMessage,
)
from .cycle_scheduler import CYCLE_NEGOTIATING
from .message_audit import OUTBOUND_PENDING
from .negotiation_rules import (
    evaluate_negotiation_offer,
)


NEGOTIATION_PENDING_PUBLICATION = (
    "PENDING_PUBLICATION"
)


def _json_number(
    value: Decimal,
) -> int | float:
    """
    Convierte Decimal a un número serializable
    en JSON.
    """

    if value == value.to_integral_value():
        return int(value)

    return float(value)


def enqueue_negotiation_proposal(
    session: Session,
    *,
    cycle_id: str,
    direction: str,
    quantity: Decimal,
    price_per_energy: Decimal,
    city_id: str,
    routing_key: str,
    commit: bool = True,
) -> Negotiation:
    """
    Crea una negociación voluntaria y deja su
    negotiation-proposal en el outbox.

    La publicación real en RabbitMQ la realiza
    el connector implementado en E1-41.
    """

    # --------------------------------------------------
    # 1. Validaciones básicas
    # --------------------------------------------------

    cycle = session.get(
        Cycle,
        cycle_id,
    )

    if cycle is None:
        raise ValueError(
            f"Cycle {cycle_id} not found"
        )

    if direction not in {
        "give",
        "take",
    }:
        raise ValueError(
            "direction must be give or take"
        )

    if quantity <= Decimal("0"):
        raise ValueError(
            "quantity must be greater than zero"
        )

    if price_per_energy < Decimal("0"):
        raise ValueError(
            "pricePerEnergy cannot be negative"
        )

    now = datetime.now(
        timezone.utc
    )

    # --------------------------------------------------
    # 2. Solo negociar dentro de la ventana
    # --------------------------------------------------

    if (
        cycle.valid_until is not None
        and now >= cycle.valid_until
    ):
        raise ValueError(
            f"Cycle {cycle_id} is expired"
        )

    if (
        cycle.scheduler_state
        != CYCLE_NEGOTIATING
    ):
        raise ValueError(
            f"Cycle {cycle_id} is not "
            "in negotiation window"
        )

    # --------------------------------------------------
    # 3. Aplicar las reglas de E1-42
    # --------------------------------------------------

    evaluate_negotiation_offer(
        session,
        cycle=cycle,
        direction=direction,
        quantity=quantity,
        price_per_energy=price_per_energy,
    )

    # --------------------------------------------------
    # 4. IDs del protocolo
    # --------------------------------------------------

    idpk = str(uuid4())
    msg_id = str(uuid4())

    # --------------------------------------------------
    # 5. Construir negotiation-proposal
    # --------------------------------------------------

    payload = {
        "idpk": idpk,
        "msgId": msg_id,
        "type": "negotiation-proposal",
        "timestamp": (
            now.isoformat()
            .replace("+00:00", "Z")
        ),
        "cityId": city_id,
        "cycleId": cycle_id,
        "data": {
            "direction": direction,
            "quantity": _json_number(
                quantity
            ),
            "pricePerEnergy": _json_number(
                price_per_energy
            ),
        },
    }

    # --------------------------------------------------
    # 6. Persistir negociación
    # --------------------------------------------------

    negotiation = Negotiation(
        cycle_id=cycle_id,
        idpk=idpk,
        latest_msg_id=msg_id,
        direction=direction,
        requested_quantity=quantity,
        offered_price=price_per_energy,

        # Todavía no sabemos si RabbitMQ
        # logró publicar.
        status=(
            NEGOTIATION_PENDING_PUBLICATION
        ),

        # Los 30 segundos deben comenzar
        # cuando realmente se publique.
        deadline_at=None,

        created_at=now,
        updated_at=now,
    )

    session.add(negotiation)

    # --------------------------------------------------
    # 7. Persistir intención de publicación
    # --------------------------------------------------

    outbound = OutboundMessage(
        msg_id=msg_id,
        idpk=idpk,
        message_type=(
            "negotiation-proposal"
        ),
        cycle_id=cycle_id,
        payload=payload,
        target_msg_id=None,
        routing_key=routing_key,
        status=OUTBOUND_PENDING,
        attempt_count=0,
        created_at=now,

        # E1-41 hará que el connector
        # recoja y publique este mensaje.
        dispatch_required=True,
    )

    session.add(outbound)

    if commit:
        session.commit()
        session.refresh(negotiation)
    else:
        session.flush()

    return negotiation