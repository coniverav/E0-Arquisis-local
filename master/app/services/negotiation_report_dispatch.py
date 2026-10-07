from datetime import datetime, timezone
from uuid import uuid4

from sqlmodel import Session, select

from ..models import Cycle, NegotiationReport, OutboundMessage
from .budget_carryover import lock_city_ledger
from .negotiation_report import generate_negotiation_report, latest_report


def _outbounds(session, report):
    return session.exec(select(OutboundMessage).where(
        OutboundMessage.idpk == report.idpk,
        OutboundMessage.message_type == "negotiation-report",
    ).order_by(OutboundMessage.id)).all()


def expire_report_dispatches(session: Session, now: datetime) -> None:
    """También se ejecuta al despachar, aunque el scheduler esté detenido."""
    cycles = session.exec(select(Cycle).where(
        Cycle.cycle_id.in_(select(OutboundMessage.cycle_id).where(
            OutboundMessage.message_type == "negotiation-report",
            OutboundMessage.dispatch_required.is_(True),
        ))
    ).with_for_update(skip_locked=True)).all()
    for cycle in cycles:
        reason = None
        if cycle.status_idpk is None or cycle.valid_until is None:
            reason = "CYCLE_UNKNOWN"
        elif cycle.scheduler_state == "CLOSED" or now >= cycle.valid_until:
            reason = "CYCLE_EXPIRED"
        if reason is None:
            continue
        pending = session.exec(select(OutboundMessage).where(
            OutboundMessage.cycle_id == cycle.cycle_id,
            OutboundMessage.message_type == "negotiation-report",
            OutboundMessage.dispatch_required.is_(True),
        )).all()
        for outbound in pending:
            outbound.dispatch_required = False
            outbound.last_error = reason
            report = session.exec(select(NegotiationReport).where(
                NegotiationReport.idpk == outbound.idpk
            )).first()
            if report is not None and report.status in {"PENDING", "DEFERRED"}:
                report.status = "EXPIRED" if reason == "CYCLE_EXPIRED" else "REJECTED"
                report.reason = reason

    session.flush()


def enqueue_due_negotiation_reports(
    session: Session, *, city_id: str, routing_key: str, now: datetime | None = None,
) -> int:
    lock_city_ledger(session)
    now = now or datetime.now(timezone.utc)
    expire_report_dispatches(session, now)
    cycles = session.exec(select(Cycle).where(
        Cycle.scheduler_state == "REPORT_WINDOW",
        Cycle.status_idpk.is_not(None),
        Cycle.report_window_opens_at <= now,
        Cycle.valid_until > now,
    ).with_for_update(skip_locked=True)).all()
    enqueued = 0
    for cycle in cycles:
        previous = latest_report(session, cycle.cycle_id)
        #Completar el intento diferido con su contenido original antes de corregirlo.
        if previous is not None and previous.status in {"DEFERRED", "EXPIRED", "REJECTED"}:
            continue
        report = generate_negotiation_report(session, cycle_id=cycle.cycle_id, city_id=city_id, now=now)
        if _outbounds(session, report):
            continue
        # Un reporte antiguo pendiente no debe reemplazar su corrección más reciente.
        for old in session.exec(select(OutboundMessage).where(
            OutboundMessage.cycle_id == cycle.cycle_id,
            OutboundMessage.message_type == "negotiation-report",
            OutboundMessage.idpk != report.idpk,
            OutboundMessage.dispatch_required.is_(True),
        )).all():
            old.dispatch_required = False
            old.last_error = "SUPERSEDED"
            old_report = session.exec(select(NegotiationReport).where(NegotiationReport.idpk == old.idpk)).first()
            if old_report is not None and old_report.status in {"PENDING", "DEFERRED"}:
                old_report.status = "SUPERSEDED"
        session.add(OutboundMessage(
            msg_id=report.msg_id, idpk=report.idpk, message_type="negotiation-report",
            cycle_id=cycle.cycle_id, payload=report.payload, routing_key=routing_key,
            status="PENDING", created_at=now, dispatch_required=True,
            available_at=cycle.report_window_opens_at, expires_at=cycle.valid_until,
        ))
        enqueued += 1
    session.flush()
    return enqueued


def process_report_error(session: Session, payload, *, now: datetime | None = None) -> None:
    """El rechazo de negocio se distingue de la publicación y del NACK.

    Cada intento diferido usa un msgId nuevo y conserva idpk y saldos.
    Un error tardío no puede reagendar un intento o una corrección posterior.
    """
    lock_city_ledger(session)
    now = now or datetime.now(timezone.utc)
    cycle = session.exec(select(Cycle).where(Cycle.cycle_id == payload.cycleId).with_for_update()).first()
    if cycle is None:
        return
    target = session.exec(select(OutboundMessage).where(
        OutboundMessage.msg_id == str(payload.data.target),
        OutboundMessage.message_type == "negotiation-report",
        OutboundMessage.cycle_id == payload.cycleId,
    ).with_for_update()).first()
    if target is None:
        return
    report = session.exec(select(NegotiationReport).where(NegotiationReport.idpk == target.idpk)).first()
    if report is None:
        return
    attempts = _outbounds(session, report)
    if attempts[-1].msg_id != target.msg_id or latest_report(session, cycle.cycle_id).id != report.id:
        return
    if report.status in {"EXPIRED", "REJECTED"}:
        return
    target.dispatch_required = False
    target.last_error = payload.reason
    report.reason = payload.reason
    if payload.reason == "CYCLE_EXPIRED" or cycle.valid_until is None or now >= cycle.valid_until:
        report.status = "EXPIRED"
        cycle.scheduler_state = "CLOSED"
        cycle.closed_at = cycle.closed_at or now
        expire_report_dispatches(session, now)
    elif payload.reason == "REPORT_TOO_EARLY":
        opens_at = payload.data.opensAt
        #Persistir la instrucción de la central aunque se repita el status-statement.
        cycle.report_window_opens_at = max(cycle.report_window_opens_at or opens_at, opens_at)
        if opens_at >= cycle.valid_until:
            report.status = "EXPIRED"
        elif opens_at <= now:
            #Una instrucción contradictoria o vencida no debe provocar reintentos continuos.
            report.status = "REJECTED"
        else:
            report.status = "DEFERRED"
            msg_id = str(uuid4())
            retry_payload = {**report.payload, "msgId": msg_id,
                             "timestamp": now.isoformat().replace("+00:00", "Z")}
            session.add(OutboundMessage(
                msg_id=msg_id, idpk=report.idpk, message_type="negotiation-report",
                cycle_id=cycle.cycle_id, payload=retry_payload, routing_key=target.routing_key,
                status="PENDING", created_at=now, dispatch_required=True,
                available_at=cycle.report_window_opens_at, expires_at=cycle.valid_until,
            ))
    else:
        report.status = "REJECTED"
    session.flush()


def mark_negotiation_report_sent(
    session: Session, *, msg_id: str, sent_at: datetime,
) -> NegotiationReport | None:
    outbound = session.exec(select(OutboundMessage).where(OutboundMessage.msg_id == msg_id)).first()
    report = session.exec(select(NegotiationReport).where(
        NegotiationReport.idpk == outbound.idpk if outbound else NegotiationReport.msg_id == msg_id
    )).first()
    if report is None:
        return None
    attempts = _outbounds(session, report)
    #Un resultado de publicación tardío no anula un rechazo ni una reprogramación.
    if (not attempts or attempts[-1].msg_id == msg_id) and not (outbound and outbound.last_error):
        if report.status in {"PENDING", "DEFERRED"}:
            report.status = "PUBLISHED"
            report.reason = None
    if not attempts or attempts[-1].msg_id == msg_id:
        report.sent_at = sent_at
    else:
        report.sent_at = report.sent_at or sent_at
    session.add(report)
    session.flush()
    return report
