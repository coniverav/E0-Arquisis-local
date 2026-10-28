import logging
import time
import asyncio
from datetime import datetime, timezone
from sqlmodel import Session, select


from ..config import (
    CITY_ID,
    CYCLE_SCHEDULER_POLL_SECONDS,
    RABBITMQ_CENTRAL_ROUTING_KEY,
)
from ..database import engine
from ..models import Cycle

from ..services.seller_offer import enqueue_seller_offer
from ..services.seller_offer_events import close_expired_local_offers
from ..services.seller_swap_dispatch import dispatch_swap_proposals

from ..services.cycle_scheduler import (
    process_cycle_windows,
)
from ..services.negotiation_report_dispatch import (
    enqueue_due_negotiation_reports,
)
from ..services.negotiation_timeouts import (
    process_expired_negotiations,
)
from ..services.negotiation_proposals import (
    enqueue_timed_out_negotiation_retries,
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

                negotiation_retries = (
                    enqueue_timed_out_negotiation_retries(
                        session,
                        city_id=CITY_ID,
                        routing_key=(
                            RABBITMQ_CENTRAL_ROUTING_KEY
                        ),
                    )
                )

                # E2-029: cerrar ofertas locales después de market.closesAt.
                # No se publica withdrawn después de market.lastOfferAt.
                local_offers_closed = close_expired_local_offers(session)
                session.commit()
                if local_offers_closed:
                    logger.info("Ofertas seller cerradas: %s", local_offers_closed)

                # Ciclos abiertos que podrían tener energía para vender.
                now = datetime.now(timezone.utc)

                offer_cycles = session.exec(
                    select(Cycle.cycle_id)
                    .where(Cycle.status_idpk.is_not(None))
                    .where(Cycle.market_opens_at.is_not(None))
                    .where(Cycle.market_opens_at <= now)
                    .where(Cycle.market_last_offer_at > now)
                    .where(Cycle.generation_capacity > Cycle.consumption)
                    .order_by(Cycle.market_opens_at.desc())
                    .limit(3)
                ).all()


                if (
                    result["cycles_prepared"]
                    or result["report_windows_opened"]
                    or result["cycles_closed"]
                    or reports_enqueued
                    or negotiations_timed_out
                    or negotiation_retries
                ):
                    logger.info(
                        (
                            "Scheduler procesó ventanas: "
                            "cycles_prepared=%s "
                            "report_windows_opened=%s "
                            "cycles_closed=%s "
                            "reports_enqueued=%s "
                            "negotiations_timed_out=%s "
                            "negotiation_retries=%s"
                        ),
                        result["cycles_prepared"],
                        result["report_windows_opened"],
                        result["cycles_closed"],
                        reports_enqueued,
                        negotiations_timed_out,
                        negotiation_retries,
                    )


            # E2-032: despachar intenciones SUBMITTED sin bloquear las ofertas
            # durante el HTTP. HTTP 202 no publica bid-accepted (E2-033).
            try:
                dispatched = asyncio.run(dispatch_swap_proposals(limit=10))
                if dispatched["claimed"]:
                    logger.info("Propuestas wallet procesadas: %s", dispatched)
            except Exception:
                logger.exception("Error despachando propuestas de swaps")

            # Crear oferta automática para cada ciclo elegible.
            # El worker revisa periódicamente los ciclos abiertos con excedente.
            # Si la wallet está temporalmente caída, registrará el error y podrá reintentarlo en el siguiente recorrido).
            for cycle_id in offer_cycles:
                try:
                    created = asyncio.run(enqueue_seller_offer(cycle_id))

                    if created:
                        logger.info(
                            "Oferta seller registrada para %s",
                            cycle_id,
                        )

                except Exception:
                    logger.exception(
                        "No se pudo crear oferta para %s",
                        cycle_id,
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
