from datetime import (
    datetime,
    timedelta,
    timezone,
)

from sqlmodel import Session

from ..models import Negotiation

NEGOTIATION_PENDING_PUBLICATION = "PENDING_PUBLICATION"
NEGOTIATION_PROPOSED = "PROPOSED"
NEGOTIATION_ACKNOWLEDGED = "ACKNOWLEDGED"
NEGOTIATION_CONFIRMED = "CONFIRMED"
NEGOTIATION_PAID = "PAID"
NEGOTIATION_REJECTED = "REJECTED"
NEGOTIATION_TIMEOUT = "TIMEOUT"

NEGOTIATION_TIMEOUT_SECONDS = 30

class InvalidNegotiationTransition(ValueError):
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
    NEGOTIATION_TIMEOUT: {
        NEGOTIATION_PENDING_PUBLICATION,
    },
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

    current_status = negotiation.status

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

    # --------------------------------------------------
    # E1-49: manejo persistente de deadlines
    # --------------------------------------------------

    # La central tiene 30 segundos desde la publicación
    # real de la propuesta para responder con give/take.
    if new_status == NEGOTIATION_PENDING_PUBLICATION:
        negotiation.deadline_at = None

    elif new_status == NEGOTIATION_PROPOSED:
        negotiation.deadline_at = (
            now
            + timedelta(
                seconds=NEGOTIATION_TIMEOUT_SECONDS
            )
        )
    # Un ACK solo confirma recepción.
    # NO reinicia el plazo de 30 segundos.
    elif new_status == NEGOTIATION_ACKNOWLEDGED:
        pass

    # Después de una confirmación GIVE debemos esperar
    # hasta 30 segundos por el transfer de la central.
    # Para TAKE nosotros pagamos inmediatamente,
    # por lo que no queda una espera externa.
    elif new_status == NEGOTIATION_CONFIRMED:

        if negotiation.direction == "give":
            negotiation.deadline_at = (
                now
                + timedelta(
                    seconds=NEGOTIATION_TIMEOUT_SECONDS
                )
            )

        else:
            negotiation.deadline_at = None

    # Si la negociación terminó correctamente o fue
    # rechazada, ya no debe existir un timeout pendiente.
    elif new_status in {NEGOTIATION_PAID, NEGOTIATION_REJECTED}:
        negotiation.deadline_at = None

    # En TIMEOUT conservamos deadline_at.
    # Esto permite explicar:
    #   deadline_at = cuándo debía llegar la respuesta
    #   updated_at  = cuándo el worker detectó el timeout
    elif new_status == NEGOTIATION_TIMEOUT:
        pass

    session.add(negotiation)
    session.flush()

    return True