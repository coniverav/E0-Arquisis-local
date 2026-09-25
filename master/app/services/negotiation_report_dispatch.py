from datetime import datetime, timezone

from sqlmodel import Session, select

from ..models import (
    Cycle,
    NegotiationReport,
    OutboundMessage,
)
from .cycle_scheduler import CYCLE_REPORT_WINDOW
from .message_audit import OUTBOUND_PENDING
from .negotiation_report import (
    generate_negotiation_report,
)


def enqueue_due_negotiation_reports(
    session: Session,
    *,
    city_id: str,
    routing_key: str,
    now: datetime | None = None,
) -> int:
    """
    Genera, si es necesario, y deja en el outbox los
    negotiation-report correspondientes a ciclos que
    están dentro de su ventana de reporte.

    La publicación real en RabbitMQ sigue siendo
    responsabilidad del connector.
    """

    if now is None:
        now = datetime.now(timezone.utc)

    cycles = session.exec(
        select(Cycle)
        .where(
            Cycle.scheduler_state
            == CYCLE_REPORT_WINDOW
        )
        .where(
            Cycle.valid_until.is_not(None)
        )
        .where(
            Cycle.valid_until > now
        )
        .with_for_update(skip_locked=True)
    ).all()

    enqueued = 0

    for cycle in cycles:
        report = session.exec(
            select(NegotiationReport).where(
                NegotiationReport.cycle_id
                == cycle.cycle_id
            )
        ).first()

        if report is None:
            report = generate_negotiation_report(
                session,
                cycle_id=cycle.cycle_id,
                city_id=city_id,
            )

        existing_outbound = session.exec(
            select(OutboundMessage).where(
                OutboundMessage.msg_id
                == report.msg_id
            )
        ).first()

        if existing_outbound is not None:
            continue

        outbound = OutboundMessage(
            msg_id=report.msg_id,
            idpk=report.idpk,
            message_type="negotiation-report",
            cycle_id=cycle.cycle_id,
            payload=report.payload,
            target_msg_id=None,
            routing_key=routing_key,
            status=OUTBOUND_PENDING,
            attempt_count=0,
            created_at=report.created_at,
            dispatch_required=True,
        )

        session.add(outbound)
        enqueued += 1

    session.flush()

    return enqueued


def mark_negotiation_report_sent(
    session: Session,
    *,
    msg_id: str,
    sent_at: datetime,
) -> NegotiationReport | None:
    """
    Sincroniza el negotiation-report con la evidencia
    de publicación registrada en el outbox.
    """

    report = session.exec(
        select(NegotiationReport).where(
            NegotiationReport.msg_id == msg_id
        )
    ).first()

    if report is None:
        return None

    if report.sent_at is None:
        report.sent_at = sent_at
        session.add(report)
        session.commit()
        session.refresh(report)

    return report
