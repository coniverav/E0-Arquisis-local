from datetime import datetime, timezone
from decimal import Decimal

from sqlmodel import Session, select

from ..models import (
    LedgerEntry,
    Negotiation,
)

from .ledger import apply_ledger_effect
from .negotiation_state import (
    NEGOTIATION_CONFIRMED,
    transition_negotiation,
)


class NegotiationConfirmationError(ValueError):
    pass


def process_negotiation_confirmation(
    session: Session,
    *,
    confirmation_type: str,
    cycle_id: str,
    idpk: str,
    msg_id: str,
    target_msg_id: str,
    energy: Decimal,
    price_per_energy: Decimal,
    details: dict,
    now: datetime | None = None,
) -> tuple[
    Negotiation,
    LedgerEntry,
    bool,
]:
    """
    Procesa una confirmación give/take recibida desde
    la central.

    Retorna:
        (negotiation, ledger_entry, True)
            si se aplicó.

        (negotiation, ledger_entry, False)
            si el efecto ya estaba aplicado.

    No realiza commit.
    """

    if now is None:
        now = datetime.now(timezone.utc)

    # -------------------------------------------------
    # 1. Validaciones básicas
    # -------------------------------------------------

    if confirmation_type not in {
        "give",
        "take",
    }:
        raise NegotiationConfirmationError(
            "confirmation type must be give or take"
        )

    if energy <= Decimal("0"):
        raise NegotiationConfirmationError(
            "confirmed energy must be positive"
        )

    if price_per_energy < Decimal("0"):
        raise NegotiationConfirmationError(
            "confirmed price cannot be negative"
        )

    # -------------------------------------------------
    # 2. Asociar con la propuesta original
    # -------------------------------------------------

    negotiation = session.exec(
        select(Negotiation)
        .where(
            Negotiation.latest_msg_id
            == target_msg_id
        )
        .with_for_update()
    ).first()

    if negotiation is None:
        raise NegotiationConfirmationError(
            "confirmation target does not match "
            "an active negotiation"
        )

    # -------------------------------------------------
    # 3. Verificar ciclo
    # -------------------------------------------------

    if negotiation.cycle_id != cycle_id:
        raise NegotiationConfirmationError(
            "confirmation cycle does not match "
            "negotiation cycle"
        )

    # -------------------------------------------------
    # 4. Verificar dirección
    # -------------------------------------------------

    if negotiation.direction != confirmation_type:
        raise NegotiationConfirmationError(
            f"{confirmation_type} confirmation "
            f"cannot confirm a "
            f"{negotiation.direction} negotiation"
        )

    # -------------------------------------------------
    # 5. Determinar efecto energético
    # -------------------------------------------------

    if confirmation_type == "give":

        # La ciudad entrega energía (-)
        energy_delta = -energy

        operation_type = (
            "GIVE_CONFIRMED"
        )

    else:

        # La ciudad recibe energía (+)
        energy_delta = energy

        operation_type = (
            "TAKE_CONFIRMED"
        )

    # -------------------------------------------------
    # 6. Aplicar efecto al ledger de forma idempotente
    #    por negociación lógica.
    # -------------------------------------------------

    existing_effect = session.exec(
        select(LedgerEntry).where(
            LedgerEntry.negotiation_id
            == negotiation.id,
            LedgerEntry.operation_type
            == operation_type,
        )
    ).first()

    if existing_effect is not None:

        #Esta negociación ya produjo su efecto energético. Un retry no puede volver a modificar el balance.
        if (
            existing_effect.energy_delta
            != energy_delta
        ):
            raise NegotiationConfirmationError(
                "retry confirmation energy does not "
                "match original confirmation"
            )

        if (
            negotiation.confirmed_price is not None
            and negotiation.confirmed_price
            != price_per_energy
        ):
            raise NegotiationConfirmationError(
                "retry confirmation price does not "
                "match original confirmation"
            )

        ledger_entry = existing_effect
        applied = False

    else:

        ledger_entry, applied = (
            apply_ledger_effect(
                session,
                cycle_id=cycle_id,
                idpk=idpk,
                source_msg_id=msg_id,
                operation_type=operation_type,

                # El pago se procesa después.
                budget_delta=Decimal("0"),

                energy_delta=energy_delta,
                details=details,

                # Asociar explícitamente el efecto
                # con esta negociación.
                negotiation_id=negotiation.id,
            )
        )

    # -------------------------------------------------
    # 7. Persistir datos de la confirmación
    # -------------------------------------------------

    negotiation.confirmed_energy = energy
    negotiation.confirmed_price = price_per_energy

    # -------------------------------------------------
    # 8. Máquina de estados
    # -------------------------------------------------

    transition_negotiation(
        session,
        negotiation,
        NEGOTIATION_CONFIRMED,
        now=now,
    )

    # -------------------------------------------------
    # 9. La próxima correlación será con el msgId
    #    de ESTA confirmación.
    #
    # transfer.data.becauseOf → este msgId
    # -------------------------------------------------

    negotiation.latest_msg_id = msg_id

    session.add(negotiation)
    session.flush()

    return (
        negotiation,
        ledger_entry,
        applied,
    )