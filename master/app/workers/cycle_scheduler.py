import logging
import time

from sqlmodel import Session

from ..config import (
    CITY_ID,
    CYCLE_SCHEDULER_POLL_SECONDS,
    RABBITMQ_CENTRAL_ROUTING_KEY,
)
from ..database import engine
from ..services.cycle_scheduler import (
    process_cycle_windows,
)
from ..services.negotiation_report_dispatch import (
    enqueue_due_negotiation_reports,
)
from ..services.negotiation_timeouts import (
    process_expired_negotiations,
)


logging.basicConfig(level=logging.INFO)

logger = logging.getLogger(
    "cycle-scheduler"
)


def run() -> None:
    logger.info(
        "Cycle scheduler iniciado; poll=%ss",
        CYCLE_SCHEDULER_POLL_SECONDS,
    )

    while True:
        try:
            with Session(engine) as session:
                result = process_cycle_windows(
                    session
                )

                reports_enqueued = (
                    enqueue_due_negotiation_reports(
                        session,
                        city_id=CITY_ID,
                        routing_key=(
                            RABBITMQ_CENTRAL_ROUTING_KEY
                        ),
                    )
                )

                # Procesar negociaciones cuyo deadline venció.
                negotiations_timed_out = (
                    process_expired_negotiations(
                        session
                    )
                )

                session.commit()

                if (
                    result["cycles_prepared"]
                    or result[
                        "report_windows_opened"
                    ]
                    or result["cycles_closed"]
                    or reports_enqueued
                    or negotiations_timed_out
                ):
                    logger.info(
                        (
                            "Scheduler procesó ventanas: "
                            "cycles_prepared=%s "
                            "report_windows_opened=%s "
                            "cycles_closed=%s "
                            "reports_enqueued=%s"
                            "negotiations_timed_out=%s"
                        ),
                        result["cycles_prepared"],
                        result["report_windows_opened"],
                        result["cycles_closed"],
                        reports_enqueued,
                        negotiations_timed_out,
                    )

        except Exception:
            logger.exception(
                "Error procesando ventanas de ciclo"
            )

        time.sleep(
            CYCLE_SCHEDULER_POLL_SECONDS
        )


if __name__ == "__main__":
    run()
