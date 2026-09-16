from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from ..database import get_session
from ..models import (
    Cycle,
    LedgerEntry,
    Negotiation,
    NegotiationReport,
)
from ..schemas import (
    CycleDetailOut,
    CycleSummaryOut,
    CyclesOut,
    LedgerEntryOut,
    NegotiationOut,
    NegotiationReportOut,
)
from ..services.ledger import rebuild_cycle_balances


router = APIRouter(
    prefix="/cycles",
    tags=["cycles"],
)


def _entry_to_out(entry: LedgerEntry) -> LedgerEntryOut:
    return LedgerEntryOut(
        sequence=entry.sequence_no,
        operationType=entry.operation_type,
        budgetDelta=entry.budget_delta,
        energyDelta=entry.energy_delta,
        budgetAfter=entry.budget_after,
        energyAfter=entry.energy_after,
        appliedAt=entry.applied_at,
        details=entry.details,
    )


def _negotiation_to_out(
    negotiation: Negotiation,
) -> NegotiationOut:
    return NegotiationOut(
        id=negotiation.id,
        idpk=negotiation.idpk,
        direction=negotiation.direction,
        requestedQuantity=negotiation.requested_quantity,
        offeredPrice=negotiation.offered_price,
        status=negotiation.status,
        confirmedEnergy=negotiation.confirmed_energy,
        confirmedPrice=negotiation.confirmed_price,
        paymentQuantity=negotiation.payment_quantity,
        deadlineAt=negotiation.deadline_at,
    )


def _report_to_out(
    report: NegotiationReport,
) -> NegotiationReportOut:
    return NegotiationReportOut(
        msgId=report.msg_id,
        idpk=report.idpk,
        budgetBalance=report.budget_balance,
        energyBalance=report.energy_balance,
        createdAt=report.created_at,
        sentAt=report.sent_at,
    )


@router.get("", response_model=CyclesOut)
def list_cycles(
    session: Session = Depends(get_session),
) -> CyclesOut:

    cycles = session.exec(
        select(Cycle).order_by(Cycle.created_at.desc())
    ).all()

    items = []

    for cycle in cycles:

        report = session.exec(
            select(NegotiationReport).where(
                NegotiationReport.cycle_id == cycle.cycle_id
            )
        ).first()

        items.append(
            CycleSummaryOut(
                cycleId=cycle.cycle_id,
                budgetBalance=cycle.budget_balance,
                energyBalance=cycle.energy_balance,
                lastOperationType=cycle.last_operation_type,
                lastOperationAt=cycle.last_operation_at,
                reported=report is not None,
            )
        )

    return CyclesOut(
        total=len(items),
        items=items,
    )


@router.get("/{cycle_id}", response_model=CycleDetailOut)
def cycle_detail(
    cycle_id: str,
    session: Session = Depends(get_session),
) -> CycleDetailOut:

    cycle = session.get(Cycle, cycle_id)

    if cycle is None:
        raise HTTPException(
            status_code=404,
            detail="Cycle not found",
        )

    # Historial append-only completo, siempre ordenado.
    entries = session.exec(
        select(LedgerEntry)
        .where(LedgerEntry.cycle_id == cycle_id)
        .order_by(LedgerEntry.sequence_no)
    ).all()

    negotiations = session.exec(
        select(Negotiation)
        .where(Negotiation.cycle_id == cycle_id)
        .order_by(Negotiation.created_at)
    ).all()

    report = session.exec(
        select(NegotiationReport).where(
            NegotiationReport.cycle_id == cycle_id
        )
    ).first()

    # Reconstrucción independiente del snapshot.
    rebuilt_budget, rebuilt_energy = rebuild_cycle_balances(
        session,
        cycle_id,
    )

    ledger_out = [
        _entry_to_out(entry)
        for entry in entries
    ]

    # Todas las transferencias que significaron entrada de fondos.
    funds_received = [
        _entry_to_out(entry)
        for entry in entries
        if entry.operation_type
        in {"TRANSFER_IN", "PAYMENT_RECEIVED"}
    ]

    demand_statements = [
        _entry_to_out(entry)
        for entry in entries
        if entry.operation_type == "DEMAND_STATEMENT"
    ]

    last_operation = (
        _entry_to_out(entries[-1])
        if entries
        else None
    )

    # Si ya se reportó el ciclo, esos son los balances finales
    # efectivamente informados a la central.
    if report is not None:
        final_budget = report.budget_balance
        final_energy = report.energy_balance
    else:
        # Si el ciclo sigue abierto mostramos el estado actual.
        final_budget = cycle.budget_balance
        final_energy = cycle.energy_balance

    return CycleDetailOut(
        cycleId=cycle.cycle_id,

        statusStatement=cycle.status_payload,

        fundsReceived=funds_received,
        demandStatements=demand_statements,

        negotiations=[
            _negotiation_to_out(n)
            for n in negotiations
        ],

        negotiationReport=(
            _report_to_out(report)
            if report is not None
            else None
        ),

        finalBudgetBalance=final_budget,
        finalEnergyBalance=final_energy,

        lastOperation=last_operation,

        reconstructedBudgetBalance=rebuilt_budget,
        reconstructedEnergyBalance=rebuilt_energy,

        snapshotConsistent=(
            rebuilt_budget == cycle.budget_balance
            and rebuilt_energy == cycle.energy_balance
        ),

        ledger=ledger_out,
    )