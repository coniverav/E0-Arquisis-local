"""Arrastra dinero, no energía, entre ciclos abiertos por la central.

El orden proviene de las fechas centrales, sin interpretar cycleId.
Los ajustes se agregan al ledger para conservar la trazabilidad.
"""
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import text
from sqlmodel import Session, select
from ..models import Cycle, LedgerEntry


def lock_city_ledger(session: Session) -> None:
    #Una base representa una ciudad. El bloqueo transaccional coordina ambas réplicas de la API y el worker.
    session.execute(text("SELECT pg_advisory_xact_lock(710001)"))


def synchronize_budget_carryover(session: Session) -> None:
    from .ledger import apply_ledger_effect
    lock_city_ledger(session)
    cycles = session.exec(select(Cycle).where(
        Cycle.status_idpk.is_not(None), Cycle.valid_until.is_not(None),
    ).order_by(Cycle.valid_until).with_for_update()).all()
    deadlines = [cycle.valid_until for cycle in cycles]
    if len(deadlines) != len(set(deadlines)):
        raise ValueError("Ambiguous cycle chronology: distinct opened cycles share validUntil")
    previous = None
    for cycle in cycles:
        credited = sum((entry.budget_delta for entry in session.exec(select(LedgerEntry).where(
            LedgerEntry.cycle_id == cycle.cycle_id,
            LedgerEntry.operation_type == "BUDGET_CARRYOVER",
        )).all()), Decimal("0"))
        expected = previous.budget_balance if previous is not None else Decimal("0")
        difference = expected - credited
        if difference:
            apply_ledger_effect(session, cycle_id=cycle.cycle_id, idpk=str(uuid4()),
                source_msg_id=None, operation_type="BUDGET_CARRYOVER",
                budget_delta=difference, energy_delta=Decimal("0"),
                details={"sourceCycleId": previous.cycle_id if previous else None,
                         "sourceSequence": previous.last_sequence if previous else None,
                         "previousCarryover": str(credited), "carryover": str(expected)},
                propagate_budget=False)
        previous = cycle
    session.flush()
