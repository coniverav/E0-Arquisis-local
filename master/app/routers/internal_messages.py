from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from ..database import get_session
from ..models import (
    Cycle,
    DistanceTable,
    Negotiation,
    NegotiationReport,
)
from ..schemas import ProtocolMessageIn
from ..services.ledger import apply_ledger_effect
from ..services.message_audit import (
    INBOUND_DUPLICATE,
    INBOUND_FAILED,
    INBOUND_NOT_PROCESSED,
    INBOUND_PROCESSED,
    mark_inbound_result,
    record_inbound_message,
)
from ..services.idempotency import claim_idpk
from ..services.cycle_scheduler import (
    configure_cycle_schedule,
)

IDEMPOTENT_MESSAGE_TYPES = {
    "status-statement",
    "transfer",
    "demand-statement",
    "distance-table",
    "negotiation-proposal",
    "ack",
    "give",
    "take",
    "negotiation-report",
}

router = APIRouter(
    prefix="/internal",
    tags=["internal"],
)


def _decimal(value) -> Decimal:
    """
    Evita construir Decimal directamente desde float.
    Decimal(str(...)) conserva mejor el valor recibido.
    """
    return Decimal(str(value))


def _require_cycle_id(
    payload: ProtocolMessageIn,
) -> str:
    if payload.cycleId is None:
        raise HTTPException(
            status_code=422,
            detail="cycleId is required",
        )

    return payload.cycleId


def _get_or_create_cycle(
    session: Session,
    cycle_id: str,
) -> Cycle:
    """
    Se crea un placeholder si otro evento del ciclo llega antes
    que el status-statement.
    """

    cycle = session.get(Cycle, cycle_id)

    if cycle is not None:
        return cycle

    cycle = Cycle(
        cycle_id=cycle_id,
        opening_budget_balance=Decimal("0"),
        opening_energy_balance=Decimal("0"),
        budget_balance=Decimal("0"),
        energy_balance=Decimal("0"),
        last_sequence=0,
        status_payload={},
        created_at=datetime.now(timezone.utc),
    )

    session.add(cycle)
    session.flush()

    return cycle


@router.post("/messages")
def ingest_protocol_message(
    payload: ProtocolMessageIn,
    session: Session = Depends(get_session),
):
    """
    Procesa mensajes E1 ya validados por el connector.

    La recepción se persiste primero como evidencia durable.
    La lógica de negocio y el resultado de auditoría se confirman
    posteriormente dentro de una misma transacción.
    """

    now = datetime.now(timezone.utc)

    # La recepción queda persistida antes de procesar el mensaje.
    audit_message = record_inbound_message(
        session,
        msg_id=str(payload.msgId) if payload.msgId is not None else None,
        idpk=str(payload.idpk) if payload.idpk is not None else None,
        message_type=payload.type,
        payload=payload.model_dump(mode="json"),
        cycle_id=payload.cycleId,
        sender=getattr(payload, "sender", None),
    )

    audit_status = INBOUND_PROCESSED

    try:
        if payload.type in IDEMPOTENT_MESSAGE_TYPES:
            claimed = claim_idpk(
                session,
                idpk=str(payload.idpk),
                msg_id=str(payload.msgId),
                message_type=payload.type,
                cycle_id=payload.cycleId,
            )

            if not claimed:
                mark_inbound_result(
                    session,
                    audit_message,
                    status=INBOUND_DUPLICATE,
                    reason="idpk already processed",
                    commit=False,
                )

                session.commit()

                return {
                    "status": "duplicate",
                    "type": payload.type,
                    "cycleId": payload.cycleId,
                }

        if payload.type == "status-statement":

            cycle_id = _require_cycle_id(payload)

            cycle = _get_or_create_cycle(
                session,
                cycle_id,
            )

            # Redelivery del mismo status-statement.
            if cycle.status_idpk == str(payload.idpk):
                mark_inbound_result(
                    session,
                    audit_message,
                    status=INBOUND_DUPLICATE,
                    reason="status-statement already applied",
                )

                return {
                    "status": "duplicate",
                    "type": payload.type,
                }

            energy = payload.data["energy"]

            generation = _decimal(
                energy["generationCapacity"]
            )

            consumption = _decimal(
                energy["consumption"]
            )

            generation_cost = _decimal(
                energy["generationCost"]
            )

            # Balance energético base del ciclo.
            new_opening_energy = generation - consumption

            # Si el ciclo placeholder ya tenía movimientos,
            # se ajusta solo la diferencia respecto de la línea base.
            difference = (
                new_opening_energy
                - cycle.opening_energy_balance
            )

            cycle.opening_energy_balance = new_opening_energy
            cycle.energy_balance += difference

            cycle.generation_capacity = generation
            cycle.consumption = consumption
            cycle.generation_cost = generation_cost

            cycle.status_idpk = str(payload.idpk)
            cycle.status_msg_id = str(payload.msgId)

            cycle.status_payload = payload.data

            valid_until = datetime.fromisoformat(
                payload.data["validUntil"].replace(
                    "Z",
                    "+00:00",
                )
            )

            configure_cycle_schedule(
                cycle,
                valid_until=valid_until,
            )

            session.add(cycle)

        elif payload.type == "transfer":

            cycle_id = _require_cycle_id(payload)

            _get_or_create_cycle(
                session,
                cycle_id,
            )

            quantity = _decimal(
                payload.data["quantity"]
            )

            because_of = payload.data.get("becauseOf")

            # Si trae cityId, corresponde a una transferencia
            # emitida por nuestra ciudad.
            if payload.cityId is not None:

                operation_type = "PAYMENT_SENT"
                budget_delta = -quantity

            else:

                # Transferencia recibida desde la central.
                operation_type = (
                    "PAYMENT_RECEIVED"
                    if because_of is not None
                    else "TRANSFER_IN"
                )

                budget_delta = quantity

            _, applied = apply_ledger_effect(
                session,
                cycle_id=cycle_id,
                idpk=str(payload.idpk),
                source_msg_id=str(payload.msgId),
                operation_type=operation_type,
                budget_delta=budget_delta,
                energy_delta=Decimal("0"),
                details=payload.model_dump(mode="json"),
            )

            if not applied:
                audit_status = INBOUND_DUPLICATE

            # Si corresponde a una negociación, se actualiza su estado.
            if because_of is not None:

                negotiation = session.exec(
                    select(Negotiation).where(
                        Negotiation.latest_msg_id
                        == str(because_of)
                    )
                ).first()

                if negotiation is not None:
                    negotiation.payment_quantity = quantity
                    negotiation.status = "PAID"
                    negotiation.updated_at = now

                    session.add(negotiation)

        # Convención:
        # energy_delta = quantity
        # budget_delta = -(quantity * value_per_kwh)
        elif payload.type == "demand-statement":

            cycle_id = _require_cycle_id(payload)

            _get_or_create_cycle(
                session,
                cycle_id,
            )

            balance = payload.data["balance"]

            quantity = _decimal(
                balance["quantity"]
            )

            value_per_kwh = _decimal(
                balance["valuePerKwh"]
            )

            # Funciona tanto para quantity positiva como negativa.
            energy_delta = quantity

            budget_delta = -(
                quantity * value_per_kwh
            )

            _, applied = apply_ledger_effect(
                session,
                cycle_id=cycle_id,
                idpk=str(payload.idpk),
                source_msg_id=str(payload.msgId),
                operation_type="DEMAND_STATEMENT",
                budget_delta=budget_delta,
                energy_delta=energy_delta,
                details=payload.model_dump(mode="json"),
            )

            if not applied:
                audit_status = INBOUND_DUPLICATE

        elif payload.type == "distance-table":

            existing = session.exec(
                select(DistanceTable).where(
                    DistanceTable.idpk == str(payload.idpk)
                )
            ).first()

            if existing is None:

                distance_table = DistanceTable(
                    msg_id=str(payload.msgId),
                    idpk=str(payload.idpk),
                    source_timestamp=payload.timestamp,
                    distances=payload.data["distances"],
                    received_at=now,
                )

                session.add(distance_table)

            else:
                audit_status = INBOUND_DUPLICATE

        elif payload.type == "negotiation-proposal":

            cycle_id = _require_cycle_id(payload)

            _get_or_create_cycle(
                session,
                cycle_id,
            )

            existing = session.exec(
                select(Negotiation).where(
                    Negotiation.idpk == str(payload.idpk)
                )
            ).first()

            if existing is None:

                negotiation = Negotiation(
                    cycle_id=cycle_id,
                    idpk=str(payload.idpk),
                    latest_msg_id=str(payload.msgId),
                    direction=payload.data["direction"],
                    requested_quantity=_decimal(
                        payload.data["quantity"]
                    ),
                    offered_price=_decimal(
                        payload.data["pricePerEnergy"]
                    ),
                    status="PROPOSED",
                    deadline_at=(
                        now + timedelta(seconds=30)
                    ),
                    created_at=now,
                    updated_at=now,
                )

                session.add(negotiation)

            else:
                audit_status = INBOUND_DUPLICATE

        elif payload.type == "ack":

            target = str(
                payload.data["target"]
            )

            negotiation = session.exec(
                select(Negotiation).where(
                    Negotiation.latest_msg_id == target
                )
            ).first()

            if negotiation is not None:
                negotiation.status = "ACKNOWLEDGED"
                negotiation.updated_at = now

                session.add(negotiation)

        elif payload.type in {"give", "take"}:

            cycle_id = _require_cycle_id(payload)

            _get_or_create_cycle(
                session,
                cycle_id,
            )

            quantity = _decimal(
                payload.data["energy"]
            )

            price = _decimal(
                payload.data["pricePerEnergy"]
            )

            if payload.type == "give":
                energy_delta = -quantity
                operation_type = "GIVE_CONFIRMED"
            else:
                energy_delta = quantity
                operation_type = "TAKE_CONFIRMED"

            _, applied = apply_ledger_effect(
                session,
                cycle_id=cycle_id,
                idpk=str(payload.idpk),
                source_msg_id=str(payload.msgId),
                operation_type=operation_type,
                budget_delta=Decimal("0"),
                energy_delta=energy_delta,
                details=payload.model_dump(mode="json"),
            )

            if not applied:
                audit_status = INBOUND_DUPLICATE

            target = str(
                payload.data["target"]
            )

            negotiation = session.exec(
                select(Negotiation).where(
                    Negotiation.latest_msg_id == target
                )
            ).first()

            if negotiation is not None:

                negotiation.status = "CONFIRMED"
                negotiation.confirmed_energy = quantity
                negotiation.confirmed_price = price

                # El transfer posterior utiliza becauseOf apuntando
                # al msgId de esta confirmación.
                negotiation.latest_msg_id = str(
                    payload.msgId
                )

                negotiation.updated_at = now

                session.add(negotiation)

        elif payload.type == "negotiation-report":

            cycle_id = _require_cycle_id(payload)

            _get_or_create_cycle(
                session,
                cycle_id,
            )

            existing = session.exec(
                select(NegotiationReport).where(
                    NegotiationReport.cycle_id == cycle_id
                )
            ).first()

            if existing is None:

                report = NegotiationReport(
                    cycle_id=cycle_id,
                    msg_id=str(payload.msgId),
                    idpk=str(payload.idpk),
                    budget_balance=_decimal(
                        payload.data["budgetBalance"]
                    ),
                    energy_balance=_decimal(
                        payload.data["energyBalance"]
                    ),
                    payload=payload.model_dump(
                        mode="json"
                    ),
                    created_at=now,
                    sent_at=now,
                )

                session.add(report)

            else:
                audit_status = INBOUND_DUPLICATE

        else:

            mark_inbound_result(
                session,
                audit_message,
                status=INBOUND_NOT_PROCESSED,
                reason="message type not processed by ledger",
            )

            return {
                "status": "not_processed_by_ledger",
                "type": payload.type,
            }

        # El efecto de negocio y su resultado de auditoría
        # quedan confirmados dentro de la misma transacción.
        mark_inbound_result(
            session,
            audit_message,
            status=audit_status,
            commit=False,
        )

        session.commit()

        return {
            "status": "ok",
            "type": payload.type,
            "cycleId": payload.cycleId,
        }

    except Exception as exc:
        session.rollback()

        # La recepción inicial ya fue persistida antes del procesamiento.
        # Después del rollback se deja evidencia del fallo.
        try:
            mark_inbound_result(
                session,
                audit_message,
                status=INBOUND_FAILED,
                reason=str(exc),
            )
        except Exception:
            session.rollback()

        raise
