from datetime import datetime, timezone
from decimal import Decimal

from sqlmodel import Session, select
from ..models import Cycle, LedgerEntry


def apply_ledger_effect(
    session: Session,
    *,
    cycle_id: str,
    idpk: str,
    source_msg_id: str | None,
    operation_type: str,
    budget_delta: Decimal,
    energy_delta: Decimal,
    details: dict,
    negotiation_id: int | None = None,
) -> tuple[LedgerEntry, bool]:
    """
    Aplica exactamente un efecto sobre el ledger.

    Retorna:
        (entry, True)  -> la operación fue aplicada.
        (entry, False) -> ya había sido aplicada; era duplicada.

    IMPORTANTE:
    Esta función NO hace commit.
    El caller decide cuándo confirmar toda la transacción.
    """


    # Bloquea el snapshot del ciclo mientras se modifica.
    # (para que 2 réplicas de master no actualicen simultáneamente el mismo ciclo y generen inconsistencias)
    cycle = session.exec(
        select(Cycle)
        .where(Cycle.cycle_id == cycle_id)
        .with_for_update()
    ).first()

    if cycle is None:
        raise ValueError(f"Cycle {cycle_id} not found")

    # Verificación de idempotencia.
    existing = session.exec(
        select(LedgerEntry).where(
            LedgerEntry.idpk == idpk,
            LedgerEntry.operation_type == operation_type,
        )
    ).first()

    if existing:
        return existing, False

    next_sequence = cycle.last_sequence + 1

    new_budget = cycle.budget_balance + budget_delta
    new_energy = cycle.energy_balance + energy_delta

    entry = LedgerEntry(
        cycle_id=cycle_id,
        sequence_no=next_sequence,
        operation_type=operation_type,
        idpk=idpk,
        source_msg_id=source_msg_id,
        negotiation_id=negotiation_id,
        budget_delta=budget_delta,
        energy_delta=energy_delta,
        budget_after=new_budget,
        energy_after=new_energy,
        details=details,
        applied_at=datetime.now(timezone.utc),
    )

    session.add(entry)

    # Actualizamos la vista materializada.
    cycle.budget_balance = new_budget
    cycle.energy_balance = new_energy
    cycle.last_sequence = next_sequence
    cycle.last_operation_type = operation_type
    cycle.last_operation_at = entry.applied_at

    # Guardamos los cambios en la base de datos, pero no hacemos commit.
    # (Envía los cambios a PostegreSQL, pero no los confirma. El caller decide cuándo confirmar toda la transacción.)
    session.flush()

    return entry, True

def rebuild_cycle_balances(
    session: Session,
    cycle_id: str,
) -> tuple[Decimal, Decimal]:
    """
    Reconstruye los saldos de presupuesto y energía de un ciclo, sumando todos los efectos del ledger.
    Retorna: (budget, energy)
    """

    cycle = session.get(Cycle, cycle_id)

    if cycle is None:
        raise ValueError(f"Cycle {cycle_id} not found")

    budget = cycle.opening_budget_balance
    energy = cycle.opening_energy_balance

    entries = session.exec(
        select(LedgerEntry)
        .where(LedgerEntry.cycle_id == cycle_id)
        .order_by(LedgerEntry.sequence_no)
    ).all()

    for entry in entries:
        budget += entry.budget_delta
        energy += entry.energy_delta

    return budget, energy


def validate_cycle_consistency(
    session: Session,
    cycle_id: str,
) -> tuple[Decimal, Decimal]:
    """
    Reconstruye el ledger y verifica que coincida con el
    estado materializado almacenado en Cycle.

    Retorna los balances reconstruidos si son consistentes.

    Lanza ValueError si existe una inconsistencia.
    """

    cycle = session.get(Cycle, cycle_id)

    if cycle is None:
        raise ValueError(
            f"Cycle {cycle_id} not found"
        )

    rebuilt_budget, rebuilt_energy = rebuild_cycle_balances(
        session,
        cycle_id,
    )

    if (
        rebuilt_budget != cycle.budget_balance
        or rebuilt_energy != cycle.energy_balance
    ):
        raise ValueError(
            f"Inconsistent ledger for {cycle_id}: "
            f"snapshot=({cycle.budget_balance}, "
            f"{cycle.energy_balance}), "
            f"rebuilt=({rebuilt_budget}, "
            f"{rebuilt_energy})"
        )

    return rebuilt_budget, rebuilt_energy