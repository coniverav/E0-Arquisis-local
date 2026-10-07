from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from sqlmodel import Session, select

from ..models import Cycle, NegotiationReport
from .ledger import validate_cycle_consistency
from .budget_carryover import lock_city_ledger


def _json_number(value: Decimal) -> int | float:
    return int(value) if value == value.to_integral_value() else float(value)


def latest_report(session: Session, cycle_id: str) -> NegotiationReport | None:
    return session.exec(select(NegotiationReport).where(
        NegotiationReport.cycle_id == cycle_id
    ).order_by(NegotiationReport.id.desc())).first()


def require_report_window(cycle: Cycle, now: datetime) -> None:
    #Un ciclo provisional o el reloj local no crean obligación de reportar.
    if cycle.status_idpk is None or cycle.valid_until is None:
        raise ValueError("CYCLE_UNKNOWN: no opening status-statement from central")
    if cycle.scheduler_state == "CLOSED" or now >= cycle.valid_until:
        raise ValueError("CYCLE_EXPIRED")
    if cycle.report_window_opens_at is None or now < cycle.report_window_opens_at:
        raise ValueError("REPORT_TOO_EARLY")


def generate_negotiation_report(
    session: Session, *, cycle_id: str, city_id: str,
    now: datetime | None = None, idpk: str | None = None,
) -> NegotiationReport:
    """Registra los saldos validados; si no cambiaron, reutiliza la última operación.

    Un idpk nuevo explícito crea una corrección, incluso con iguales saldos.
    Un idpk existente devuelve su operación original. El llamador confirma la transacción.
    """
    now = now or datetime.now(timezone.utc)
    lock_city_ledger(session)
    cycle = session.exec(select(Cycle).where(Cycle.cycle_id == cycle_id).with_for_update()).first()
    if cycle is None:
        raise ValueError(f"Cycle {cycle_id} not found")
    if idpk is not None:
        existing = session.exec(select(NegotiationReport).where(NegotiationReport.idpk == idpk)).first()
        if existing is not None:
            if existing.cycle_id != cycle_id:
                raise ValueError("idpk belongs to another cycle")
            return existing
    require_report_window(cycle, now)
    budget, energy = validate_cycle_consistency(session, cycle_id)
    previous = latest_report(session, cycle_id)
    if idpk is None and previous is not None and (previous.budget_balance, previous.energy_balance) == (budget, energy):
        return previous
    idpk = idpk or str(uuid4())
    msg_id = str(uuid4())
    while msg_id == idpk:
        msg_id = str(uuid4())
    payload = {
        "idpk": idpk, "msgId": msg_id, "type": "negotiation-report",
        "timestamp": now.isoformat().replace("+00:00", "Z"),
        "cityId": city_id, "cycleId": cycle_id,
        "data": {"budgetBalance": _json_number(budget), "energyBalance": _json_number(energy)},
    }
    report = NegotiationReport(cycle_id=cycle_id, msg_id=msg_id, idpk=idpk,
        budget_balance=budget, energy_balance=energy, payload=payload, created_at=now)
    session.add(report)
    session.flush()
    return report
