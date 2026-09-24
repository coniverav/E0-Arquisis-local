import logging
import time

from sqlmodel import Session

from ..config import CYCLE_SCHEDULER_POLL_SECONDS
from ..database import engine
from ..services.cycle_scheduler import (
    process_cycle_windows,
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

                session.commit()

                if (
                    result["cycles_prepared"]
                    or result["report_windows_opened"]
                    or result["cycles_closed"]
                ):
                    logger.info(
                        (
                            "Scheduler procesó ventanas: "
                            "cycles_prepared=%s "
                            "report_windows_opened=%s "
                            "cycles_closed=%s"
                        ),
                        result["cycles_prepared"],
                        result["report_windows_opened"],
                        result["cycles_closed"],
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