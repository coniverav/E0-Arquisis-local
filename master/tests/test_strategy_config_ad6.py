"""E2-026: tests de persistencia por ciclo en PostgreSQL (AD6)."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlmodel import Session

from app.database import engine, run_migrations
from app.models import Cycle
from app.services.strategy_config import (
    bind_strategy_to_cycle,
    create_pending_strategy,
    get_cycle_strategy,
)


def _cycle(session: Session, opened_at: datetime) -> Cycle:
    cycle = Cycle(cycle_id=f"ad6-{uuid4()}", created_at=datetime.now(timezone.utc))
    cycle.market_opens_at = opened_at
    session.add(cycle)
    session.flush()
    return cycle


def test_ad6_activacion_en_siguiente_ciclo_y_version_inmutable():
    run_migrations()
    with Session(engine) as session:
        # Rollback al salir del test: no altera la configuración productiva.
        original_open = datetime.now(timezone.utc) - timedelta(minutes=1)
        current = _cycle(session, original_open)
        initial = bind_strategy_to_cycle(session, current)
        v_current = initial.version

        pending = create_pending_strategy(
            session,
            offer_price_markup=Decimal("1.1500"),
            offer_quantity_fraction=Decimal("0.8000"),
            bid_acceptance_policy="FIFO",
            offer_selection_policy="CHEAPEST_EFFECTIVE_PRICE",
            span_seconds=120,
            created_by="test",
        )
        assert pending.status == "PENDING"
        assert get_cycle_strategy(session, current.cycle_id).version == v_current

        nxt = _cycle(session, datetime.now(timezone.utc) + timedelta(minutes=1))
        activated = bind_strategy_to_cycle(session, nxt)
        assert activated.version == pending.version
        assert activated.status == "ACTIVE"
        assert activated.effective_from_cycle_id == nxt.cycle_id
        assert get_cycle_strategy(session, nxt.cycle_id).span_seconds == 120
        assert get_cycle_strategy(session, current.cycle_id).version == v_current

        # Editar durante el ciclo no cambia su versión, ni aunque se asocie otra vez.
        newer = create_pending_strategy(
            session,
            offer_price_markup=Decimal("1.1000"),
            offer_quantity_fraction=Decimal("0.9000"),
            bid_acceptance_policy="FIFO",
            offer_selection_policy="CHEAPEST_EFFECTIVE_PRICE",
            span_seconds=30,
        )
        assert newer.status == "PENDING"
        assert bind_strategy_to_cycle(session, nxt).version == activated.version
        session.rollback()


@pytest.mark.parametrize("span", [-1, 1.5, True])
def test_ad6_rechaza_span_invalido(span):
    run_migrations()
    with Session(engine) as session:
        with pytest.raises(ValueError):
            create_pending_strategy(
                session,
                offer_price_markup=Decimal("1.2"),
                offer_quantity_fraction=Decimal("1"),
                bid_acceptance_policy="FIFO",
                offer_selection_policy="CHEAPEST_EFFECTIVE_PRICE",
                span_seconds=span,
            )
        session.rollback()


def test_ad6_status_tardio_no_recibe_estrategia_del_ciclo_siguiente():
    run_migrations()
    with Session(engine) as session:
        # Los horarios de la central pueden llegar desordenados.
        inicio = datetime.now(timezone.utc) - timedelta(minutes=2)
        primero = _cycle(session, inicio)
        inicial = bind_strategy_to_cycle(session, primero)
        nueva = create_pending_strategy(
            session,
            offer_price_markup=Decimal("1.1500"),
            offer_quantity_fraction=Decimal("0.8000"),
            bid_acceptance_policy="FIFO",
            offer_selection_policy="CHEAPEST_EFFECTIVE_PRICE",
            span_seconds=120,
        )
        siguiente = _cycle(session, datetime.now(timezone.utc) + timedelta(minutes=1))
        assert bind_strategy_to_cycle(session, siguiente).version == nueva.version
        assert nueva.status == "ACTIVE"

        # El status tardio corresponde a un ciclo abierto antes de la edicion.
        tardio = _cycle(session, inicio + timedelta(seconds=30))
        anterior = bind_strategy_to_cycle(session, tardio)
        assert anterior.version == inicial.version
        assert get_cycle_strategy(session, tardio.cycle_id).span_seconds == 60
        assert nueva.status == "ACTIVE"  # no retroceder el estado global
        session.rollback()
