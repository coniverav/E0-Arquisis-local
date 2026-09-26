from datetime import (
    datetime,
    timedelta,
    timezone,
)

from sqlmodel import Session

from ..models import Negotiation


NEGOTIATION_PENDING_PUBLICATION = (
    "PENDING_PUBLICATION"
)

NEGOTIATION_PROPOSED = "PROPOSED"

NEGOTIATION_ACKNOWLEDGED = (
    "ACKNOWLEDGED"
)

NEGOTIATION_CONFIRMED = "CONFIRMED"

NEGOTIATION_PAID = "PAID"

NEGOTIATION_REJECTED = "REJECTED"

NEGOTIATION_TIMEOUT = "TIMEOUT"


CONFIRMATION_TIMEOUT_SECONDS = 30


class InvalidNegotiationTransition(
    ValueError
):
    pass


ALLOWED_TRANSITIONS = {

    NEGOTIATION_PENDING_PUBLICATION: {
        NEGOTIATION_PROPOSED,
    },

    NEGOTIATION_PROPOSED: {
        NEGOTIATION_ACKNOWLEDGED,
        NEGOTIATION_CONFIRMED,
        NEGOTIATION_REJECTED,
        NEGOTIATION_TIMEOUT,
    },

    NEGOTIATION_ACKNOWLEDGED: {
        NEGOTIATION_CONFIRMED,
        NEGOTIATION_REJECTED,
        NEGOTIATION_TIMEOUT,
    },

    NEGOTIATION_CONFIRMED: {
        NEGOTIATION_PAID,
        NEGOTIATION_TIMEOUT,
    },

    NEGOTIATION_PAID: set(),

    NEGOTIATION_REJECTED: set(),

    # E1-50 podrá extender esta transición
    # para realizar el retry con el mismo idpk.
    NEGOTIATION_TIMEOUT: set(),
}


# Orden utilizado para ignorar respuestas antiguas
# sin hacer retroceder la negociación.
STATE_ORDER = {
    NEGOTIATION_PENDING_PUBLICATION: 0,
    NEGOTIATION_PROPOSED: 1,
    NEGOTIATION_ACKNOWLEDGED: 2,
    NEGOTIATION_CONFIRMED: 3,
    NEGOTIATION_PAID: 4,
}


def transition_negotiation(
    session: Session,
    negotiation: Negotiation,
    new_status: str,
    *,
    now: datetime | None = None,
) -> bool:
    """
    Aplica una transición válida a la negociación.

    Retorna:
        True  -> cambió de estado
        False -> ya estaba en ese estado o llegó
                 un evento atrasado.

    No hace commit.
    """

    if now is None:
        now = datetime.now(
            timezone.utc
        )

    current_status = (
        negotiation.status
    )

    # Idempotencia.
    if current_status == new_status:
        return False

    allowed = ALLOWED_TRANSITIONS.get(
        current_status
    )

    if allowed is None:
        raise InvalidNegotiationTransition(
            f"Unknown negotiation state: "
            f"{current_status}"
        )

    # --------------------------------------------------
    # Evitar regresiones por mensajes atrasados
    # --------------------------------------------------

    if (
        current_status in STATE_ORDER
        and new_status in STATE_ORDER
        and STATE_ORDER[new_status]
        < STATE_ORDER[current_status]
    ):
        return False

    # --------------------------------------------------
    # Validar transición
    # --------------------------------------------------

    if new_status not in allowed:
        raise InvalidNegotiationTransition(
            f"Invalid transition: "
            f"{current_status} -> "
            f"{new_status}"
        )

    negotiation.status = new_status
    negotiation.updated_at = now

    # Los 30 segundos comienzan después
    # de publicar realmente la propuesta.
    if new_status == NEGOTIATION_PROPOSED:
        negotiation.deadline_at = (
            now
            + timedelta(
                seconds=(
                    CONFIRMATION_TIMEOUT_SECONDS
                )
            )
        )

    session.add(negotiation)
    session.flush()

    return True