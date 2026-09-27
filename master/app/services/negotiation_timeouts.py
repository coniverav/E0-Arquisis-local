from datetime import datetime, timezone

from sqlmodel import Session, select

from ..models import Negotiation

from .negotiation_state import (
    NEGOTIATION_ACKNOWLEDGED,
    NEGOTIATION_CONFIRMED,
    NEGOTIATION_PROPOSED,
    NEGOTIATION_TIMEOUT,
    transition_negotiation,
)


TIMEOUT_PENDING_STATES = (
    NEGOTIATION_PROPOSED,
    NEGOTIATION_ACKNOWLEDGED,
    NEGOTIATION_CONFIRMED,
)


def process_expired_negotiations(
    session: Session,
    *,
    now: datetime | None = None,
) -> int:
    """
    Busca negociaciones cuyo deadline_at venció
    y las mueve a TIMEOUT.

    No realiza commit.
    El worker controla la transacción.

    Retorna la cantidad de negociaciones que
    cambiaron a TIMEOUT.
    """

    if now is None:
        now = datetime.now(
            timezone.utc
        )

    negotiations = session.exec(
        select(Negotiation)
        .where(
            Negotiation.deadline_at.is_not(
                None
            ),
            Negotiation.deadline_at <= now,
            Negotiation.status.in_(
                TIMEOUT_PENDING_STATES
            ),
        )
        .with_for_update(skip_locked=True) # Proteger de que otro worker tome la misma negociación
    ).all()

    timed_out = 0

    for negotiation in negotiations:

        # --------------------------------------------------
        # Protección para TAKE confirmado
        # --------------------------------------------------
        #
        # E1-47 crea el pago inmediatamente.
        # Un TAKE confirmado no espera otra respuesta
        # de la central.
        #
        # Además limpiamos deadlines antiguos que hayan
        # quedado persistidos antes de implementar E1-49.
        # --------------------------------------------------

        if (
            negotiation.status
            == NEGOTIATION_CONFIRMED
            and negotiation.direction == "take"
        ):

            negotiation.deadline_at = None

            session.add(
                negotiation
            )

            continue

        # --------------------------------------------------
        # Marcar timeout
        # --------------------------------------------------

        changed = transition_negotiation(
            session,
            negotiation,
            NEGOTIATION_TIMEOUT,
            now=now,
        )

        if changed:
            timed_out += 1

    session.flush()

    return timed_out