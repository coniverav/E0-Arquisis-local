from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from ..database import get_session
from ..config import (
    CITY_ID,
    RABBITMQ_CENTRAL_ROUTING_KEY,
)
from ..models import (
    Cycle,
    DistanceTable,
    Negotiation,
    NegotiationReport,
)
from ..schemas import ProtocolMessageIn
from ..services.negotiation_correlation import proposal_negotiation, stop_proposal_dispatches
from ..services.budget_carryover import lock_city_ledger, synchronize_budget_carryover
from ..services.ledger import apply_ledger_effect
from ..services.negotiation_report import require_report_window
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
from ..services.information_requests import (
    enqueue_information_request,
    resolve_information_request,
)
from ..services.negotiation_state import (
    NEGOTIATION_ACKNOWLEDGED,
    NEGOTIATION_CONFIRMED,
    NEGOTIATION_PAID,
    transition_negotiation,
)
from ..services.negotiation_confirmations import (
    process_negotiation_confirmation,
)
from ..services.negotiation_payments import (
    enqueue_take_payment,
    process_give_payment,
)

IDEMPOTENT_MESSAGE_TYPES = {
    "status-statement",
    "transfer",
    "demand-statement",
    "distance-table",
    "negotiation-proposal",
    "ack",
    "nack",
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
        lock_city_ledger(session)
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

        # Si llega un mensaje asociado a un ciclo antes de recibir
        # su status-statement, se solicita el estado faltante.
        if (
            payload.cycleId is not None
            and payload.type != "status-statement"
        ):
            cycle = _get_or_create_cycle(
                session,
                payload.cycleId,
            )

            if cycle.status_idpk is None:
                enqueue_information_request(
                    session,
                    ask="status-statement",
                    city_id=CITY_ID,
                    routing_key=RABBITMQ_CENTRAL_ROUTING_KEY,
                    commit=False,
                )

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
            session.flush()
            synchronize_budget_carryover(session)

            resolve_information_request(
                session,
                response_type="status-statement",
                response_msg_id=str(payload.msgId),
                response_idpk=str(payload.idpk),
            )

        elif payload.type == "transfer":

            cycle_id = _require_cycle_id(payload)
            quantity = _decimal(payload.data["quantity"])
            because_of = payload.data.get("becauseOf")

            # ======================================================
            # transfer asociado a una negociación GIVE
            # ======================================================

            if because_of is not None:

                _, _, applied = process_give_payment(
                    session,
                    cycle_id=cycle_id,
                    idpk=str(payload.idpk),
                    msg_id=str(payload.msgId),
                    because_of=str(because_of),
                    quantity=quantity,
                    details=payload.model_dump(mode="json"),
                    now=now,
                )

                if not applied:
                    audit_status = (
                        INBOUND_DUPLICATE
                    )

            # ======================================================
            # transfer normal recibido de la central
            # ======================================================

            else:

                _get_or_create_cycle(
                    session,
                    cycle_id,
                )

                _, applied = apply_ledger_effect(
                    session,
                    cycle_id=cycle_id,
                    idpk=str(payload.idpk),
                    source_msg_id=str(payload.msgId),
                    operation_type="TRANSFER_IN",
                    budget_delta=quantity,
                    energy_delta=Decimal("0"),
                    details=payload.model_dump(mode="json"),
                )

                if not applied:
                    audit_status = (
                        INBOUND_DUPLICATE
                    )

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

            resolve_information_request(
                session,
                response_type="distance-table",
                response_msg_id=str(payload.msgId),
                response_idpk=str(payload.idpk),
                )

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

            negotiation = proposal_negotiation(session, target)

            if negotiation is not None:
                transition_negotiation(
                    session,
                    negotiation,
                    NEGOTIATION_ACKNOWLEDGED,
                    now=now,
                )

        elif payload.type == "nack":
            negotiation = proposal_negotiation(session, str(payload.data["target"]))
            if negotiation is not None and negotiation.status in {"PENDING_PUBLICATION", "PROPOSED", "ACKNOWLEDGED", "TIMEOUT"}:
                transition_negotiation(session, negotiation, "REJECTED", now=now)
                stop_proposal_dispatches(session, negotiation)

        elif payload.type in {"give", "take"}:

            cycle_id = _require_cycle_id(payload)
            quantity = _decimal(payload.data["energy"])
            price = _decimal(payload.data["pricePerEnergy"])
            target = str(payload.data["target"])

            # Retorna: (negotiation, ledger_entry, True/False)
            negotiation, _, applied = ( 
                process_negotiation_confirmation(
                    session,
                    confirmation_type=payload.type,
                    cycle_id=cycle_id,
                    idpk=str(payload.idpk),
                    msg_id=str(payload.msgId),
                    target_msg_id=target,
                    energy=quantity,
                    price_per_energy=price,
                    details=payload.model_dump(mode="json"),
                    now=now,
                )
            )
            
            if not applied:
                audit_status = (
                    INBOUND_DUPLICATE
                )

            elif payload.type == "take":
                enqueue_take_payment(
                    session,
                    negotiation_id=negotiation.id,
                    city_id=CITY_ID,
                    routing_key=RABBITMQ_CENTRAL_ROUTING_KEY,
                    now=now,
                )

        elif payload.type == "negotiation-report":

            cycle_id = _require_cycle_id(payload)

            cycle = session.exec(select(Cycle).where(Cycle.cycle_id == cycle_id).with_for_update()).first()
            try:
                require_report_window(cycle, now)
            except ValueError as exc:
                reason = str(exc).split(":")[0]
                # Validación HTTP interna; no representa un error AMQP emitido por la central.
                raise HTTPException(status_code=410 if reason == "CYCLE_EXPIRED" else 422,
                                    detail={"reason": reason, "opensAt": cycle.report_window_opens_at.isoformat() if cycle is not None and cycle.report_window_opens_at else None}) from exc
            existing = session.exec(select(NegotiationReport).where(
                NegotiationReport.idpk == str(payload.idpk)
            )).first()
            if existing is None:
                session.add(NegotiationReport(
                    cycle_id=cycle_id, msg_id=str(payload.msgId), idpk=str(payload.idpk),
                    budget_balance=_decimal(payload.data["budgetBalance"]),
                    energy_balance=_decimal(payload.data["energyBalance"]),
                    payload=payload.model_dump(mode="json"), created_at=now,
                    sent_at=now, status="PUBLISHED",
                ))
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

        if isinstance(exc, ValueError):
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        raise
