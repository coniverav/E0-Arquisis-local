from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from ..config import CYCLE_REPORT_WINDOW_SECONDS
from ..models import Cycle


CYCLE_PENDING = "PENDING"
CYCLE_NEGOTIATING = "NEGOTIATING"
CYCLE_REPORT_WINDOW = "REPORT_WINDOW"
CYCLE_CLOSED = "CLOSED"


def configure_cycle_schedule(
    cycle: Cycle,
    *,
    valid_until: datetime,
) -> None:
    """
    Configura los tiempos del ciclo a partir del validUntil
    entregado por la central.

    La transición desde PENDING es responsabilidad del scheduler.
    """

    cycle.valid_until = valid_until

    cycle.report_window_opens_at = (
        valid_until
        - timedelta(
            seconds=CYCLE_REPORT_WINDOW_SECONDS
        )
    )


def process_cycle_windows(
    session: Session,
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    """
    Avanza los ciclos cuya ventana temporal corresponda.

    No genera ni publica negotiation-report.
    E1-40 y E1-41 manejan esas responsabilidades.
    """

    if now is None:
        now = datetime.now(timezone.utc)

    cycles = session.exec(
        select(Cycle)
        .where(Cycle.valid_until.is_not(None))
        .where(Cycle.scheduler_state != CYCLE_CLOSED)
        .with_for_update(skip_locked=True)
    ).all()

    cycles_prepared = 0
    report_windows_opened = 0
    cycles_closed = 0

    for cycle in cycles:
        if cycle.report_window_opens_at is None:
            cycle.report_window_opens_at = (
                cycle.valid_until
                - timedelta(
                    seconds=CYCLE_REPORT_WINDOW_SECONDS
                )
            )

        if now >= cycle.valid_until:
            cycle.scheduler_state = CYCLE_CLOSED

            if cycle.report_window_opened_at is None:
                cycle.report_window_opened_at = (
                    cycle.report_window_opens_at
                )

            if cycle.closed_at is None:
                cycle.closed_at = cycle.valid_until

            cycles_closed += 1

        elif (
            now >= cycle.report_window_opens_at
            and cycle.scheduler_state
            != CYCLE_REPORT_WINDOW
        ):
            cycle.scheduler_state = CYCLE_REPORT_WINDOW

            if cycle.report_window_opened_at is None:
                cycle.report_window_opened_at = (
                    cycle.report_window_opens_at
                )

            report_windows_opened += 1

        elif cycle.scheduler_state == CYCLE_PENDING:
            cycle.scheduler_state = CYCLE_NEGOTIATING
            cycles_prepared += 1

        session.add(cycle)

    session.flush()

    return {
        "cycles_prepared": cycles_prepared,
        "report_windows_opened": report_windows_opened,
        "cycles_closed": cycles_closed,
    }