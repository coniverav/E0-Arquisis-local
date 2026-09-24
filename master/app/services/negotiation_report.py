from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from sqlmodel import Session, select

from ..models import (
    Cycle,
    NegotiationReport,
)
from .ledger import validate_cycle_consistency

# Convierte Decimal a un número JSON (int o float según corresponda).
def _json_number(value: Decimal) -> int | float:
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def generate_negotiation_report(
    session: Session,
    *,
    cycle_id: str,
    city_id: str,
) -> NegotiationReport:
    """
    Genera y persiste el negotiation-report correspondiente
    al estado actual del ledger.

    IMPORTANTE:
    Esta función NO publica el mensaje en RabbitMQ.
    Eso corresponde a E1-41.

    Si el reporte ya fue generado, devuelve el existente
    en lugar de generar otro.
    """

    # -----------------------------------------------------
    # 1. Buscar ciclo
    # -----------------------------------------------------

    cycle = session.exec(
        select(Cycle)
        .where(Cycle.cycle_id == cycle_id)
        .with_for_update()
    ).first()

    if cycle is None:
        raise ValueError(
            f"Cycle {cycle_id} not found"
        )

    # -----------------------------------------------------
    # 2. Evitar generar dos reports para el mismo ciclo
    # -----------------------------------------------------

    existing_report = session.exec(
        select(NegotiationReport).where(
            NegotiationReport.cycle_id == cycle_id
        )
    ).first()

    if existing_report is not None:
        return existing_report

    # -----------------------------------------------------
    # 3. Reconstruir y validar ledger 
    # (no se iguala directamente a los balances del ciclo, porque podrían haber sido modificados por un bug)
    # -----------------------------------------------------

    budget_balance, energy_balance = (
        validate_cycle_consistency(
            session,
            cycle_id,
        )
    )

    # -----------------------------------------------------
    # 4. Crear envelope del protocolo E1
    # -----------------------------------------------------

    msg_id = str(uuid4())
    idpk = str(uuid4())

    now = datetime.now(timezone.utc)

    payload = {
        "idpk": idpk,
        "msgId": msg_id,
        "type": "negotiation-report",
        "timestamp": now.isoformat().replace(
            "+00:00",
            "Z",
        ),
        "cityId": city_id,
        "cycleId": cycle_id,
        "data": {
            "budgetBalance": _json_number(
                budget_balance
            ),
            "energyBalance": _json_number(
                energy_balance
            ),
        },
    }

    # -----------------------------------------------------
    # 5. Persistir exactamente lo generado
    # -----------------------------------------------------

    report = NegotiationReport(
        cycle_id=cycle_id,
        msg_id=msg_id,
        idpk=idpk,

        budget_balance=budget_balance,
        energy_balance=energy_balance,

        payload=payload,

        created_at=now,

        # Todavía NO se ha enviado.
        # E1-41 será responsable de actualizar esto.
        sent_at=None,
    )

    session.add(report)

    # No hacemos commit dentro del servicio.
    # El caller controla la transacción.
    session.flush()

    return report