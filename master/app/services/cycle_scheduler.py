from datetime import datetime, timedelta, timezone
from typing import Any

from sqlmodel import Session, select

from ..config import CYCLE_REPORT_WINDOW_SECONDS
from .budget_carryover import lock_city_ledger
from ..models import Cycle


CYCLE_PENDING = "PENDING"
CYCLE_NEGOTIATING = "NEGOTIATING"
CYCLE_REPORT_WINDOW = "REPORT_WINDOW"
CYCLE_CLOSED = "CLOSED"


def configure_cycle_schedule(
    cycle: Cycle,
    *,
    valid_until: datetime,
    market_times: dict[str, Any] | None = None,
) -> None:
    """
    Configura los tiempos del ciclo a partir de los timestamps oficiales
    entregados por la central en status-statement (E2-023, G03, RF01).

    Nunca se calculan a partir del ID del ciclo ni se asume reloj local.
    """

    cycle.valid_until = valid_until

    if market_times and isinstance(market_times, dict):
        def _parse_ts(key: str) -> datetime | None:
            val = market_times.get(key)
            if isinstance(val, str):
                try:
                    return datetime.fromisoformat(val.replace("Z", "+00:00"))
                except ValueError:
                    return None
            return None

        opens_at = _parse_ts("opensAt")
        last_offer = _parse_ts("lastOfferAt")
        last_bid = _parse_ts("lastBidAt")
        closes_at = _parse_ts("closesAt")
        report_opens = _parse_ts("reportOpensAt")

        if opens_at:
            cycle.market_opens_at = opens_at
        if last_offer:
            cycle.market_last_offer_at = last_offer
        if last_bid:
            cycle.market_last_bid_at = last_bid
        if closes_at:
            cycle.market_closes_at = closes_at
        if report_opens:
            cycle.report_window_opens_at = report_opens

    # Si report_window_opens_at no vino explícito en market, usar ventana configurada
    if cycle.report_window_opens_at is None:
        cycle.report_window_opens_at = valid_until - timedelta(
            seconds=CYCLE_REPORT_WINDOW_SECONDS
        )
    else:
        cycle.report_window_opens_at = max(
            cycle.report_window_opens_at,
            valid_until - timedelta(seconds=CYCLE_REPORT_WINDOW_SECONDS)
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

    lock_city_ledger(session)

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