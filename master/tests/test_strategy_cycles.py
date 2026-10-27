
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from sqlmodel import Session, select

from app.database import engine
from app.models import Cycle, StrategyConfig
from app.services.strategy_cycles import bind_strategy_to_cycle


def test_new_strategy_applies_only_to_next_cycle():
    now = datetime.now(timezone.utc)

    with Session(engine) as session:
        last_version = session.exec(
            select(StrategyConfig.version)
            .order_by(StrategyConfig.version.desc())
        ).first() or 0

        v1 = last_version + 1
        v2 = last_version + 2

        # Primera configuración pendiente.
        session.add(StrategyConfig(
            version=v1,
            status="PENDING",
            offer_price_markup=Decimal("1.20"),
            offer_quantity_fraction=Decimal("0.90"),
            span_seconds=60,
            created_at=now,
            created_by="pytest",
        ))

        cycle1 = Cycle(
            cycle_id=f"ad6-test-{uuid4()}",
            created_at=now,
        )
        session.add(cycle1)

        selected1 = bind_strategy_to_cycle(session, cycle1)

        assert selected1.version == v1
        assert cycle1.strategy_version == v1

        # Una modificación crea una NUEVA versión pendiente.
        session.add(StrategyConfig(
            version=v2,
            status="PENDING",
            offer_price_markup=Decimal("1.30"),
            offer_quantity_fraction=Decimal("1.00"),
            span_seconds=120,
            created_at=now,
            created_by="pytest",
        ))
        session.flush()

        # El primer ciclo sigue usando la versión original.
        assert cycle1.strategy_version == v1

        # Al recibir el siguiente ciclo se activa la nueva.
        cycle2 = Cycle(
            cycle_id=f"ad6-test-{uuid4()}",
            created_at=now,
        )
        session.add(cycle2)

        selected2 = bind_strategy_to_cycle(session, cycle2)

        assert selected2.version == v2
        assert cycle2.strategy_version == v2

        assert cycle1.strategy_version == v1
        assert selected1.status == "ARCHIVED"
        assert selected2.status == "ACTIVE"

        # No conservar datos ni cambios de esta prueba.
        session.rollback()
