"""E2-065: generar snapshots históricos a partir de facts ya persistidos.

Ejecutar después de alembic upgrade head, sin alterar datos económicos.
Puede ejecutarse varias veces; hay una fila por ciclo.
"""
from datetime import datetime, timezone
import logging

from sqlmodel import Session, select

from ..database import engine
from ..models import Cycle
from ..services.cycle_metrics import refresh_cycle_metrics

logger = logging.getLogger(__name__)


def main() -> None:
    with Session(engine) as session:
        ids = session.exec(select(Cycle.cycle_id).where(
            Cycle.status_idpk.is_not(None), Cycle.strategy_version.is_not(None),
        ).order_by(Cycle.created_at, Cycle.cycle_id)).all()
    count = 0
    for cycle_id in ids:
        with Session(engine) as session:
            if refresh_cycle_metrics(session, cycle_id=cycle_id,
                                     now=datetime.now(timezone.utc)) is not None:
                session.commit()
                count += 1
    logger.warning("E2-065: métricas actualizadas para %s ciclos", count)
    print(f"E2-065: {count} ciclos con métricas persistidas")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
