"""E2-026 / AD6: estrategias versionadas con vigencia por ciclo.

Persistencia compartida en PostgreSQL. Todas las operaciones participan en la
transacción entregada por el llamador. No hay caches ni commits implícitos.
"""
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlmodel import Session, select

from ..models import Cycle, StrategyConfig
from .budget_carryover import lock_city_ledger


BID_POLICIES = frozenset({"FIFO", "MAX_REMAINING", "BATCH"})
SELECTION_POLICIES = frozenset({"CHEAPEST_EFFECTIVE_PRICE", "MIN_DISTANCE"})


class StrategyVersionConflict(Exception):
    """La interfaz editó una versión que dejó de ser la más reciente."""

    def __init__(self, *, expected: int, actual: int) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(f"Version obsoleta: se esperaba {expected}, actual {actual}")


def _latest(session: Session, status: Optional[str] = None) -> Optional[StrategyConfig]:
    query = select(StrategyConfig)
    if status is not None:
        query = query.where(StrategyConfig.status == status)
    return session.exec(query.order_by(StrategyConfig.version.desc())).first()


def _ensure_initial_strategy(session: Session) -> StrategyConfig:
    """Crea la versión base si aún no hay configuración en la ciudad."""
    existing = _latest(session)
    if existing is not None:
        active = _latest(session, "ACTIVE")
        return active or existing

    initial = StrategyConfig(
        version=1,
        status="ACTIVE",
        offer_price_markup=Decimal("1.2000"),
        offer_quantity_fraction=Decimal("1.0000"),
        bid_acceptance_policy="FIFO",
        offer_selection_policy="CHEAPEST_EFFECTIVE_PRICE",
        span_seconds=60,
        created_at=datetime.now(timezone.utc),
        created_by="system",
    )
    session.add(initial)
    session.flush()
    return initial


def create_pending_strategy(
    session: Session,
    *,
    offer_price_markup: Decimal,
    offer_quantity_fraction: Decimal,
    bid_acceptance_policy: str,
    offer_selection_policy: str,
    span_seconds: int,
    created_by: Optional[str] = None,
    expected_version: Optional[int] = None,
) -> StrategyConfig:
    """Crea una versión PENDING; no altera ciclos abiertos ni versiones previas.

    El endpoint protegido de E2-062 puede llamar a esta función y hacer commit
    al finalizar su transacción. Se serializa con la apertura del ciclo para
    evitar duplicados de version y carreras entre master/master2.
    """
    markup = Decimal(str(offer_price_markup))
    fraction = Decimal(str(offer_quantity_fraction))
    if not markup.is_finite() or markup <= 0:
        raise ValueError("offer_price_markup debe ser positivo y finito")
    if not fraction.is_finite() or not Decimal("0") <= fraction <= Decimal("1"):
        raise ValueError("offer_quantity_fraction debe estar entre 0 y 1")
    if bid_acceptance_policy not in BID_POLICIES:
        raise ValueError("politica de aceptacion desconocida")
    if offer_selection_policy not in SELECTION_POLICIES:
        raise ValueError("politica de seleccion desconocida")
    if type(span_seconds) is not int or span_seconds < 0:
        raise ValueError("span_seconds debe ser un entero no negativo")

    lock_city_ledger(session)
    _ensure_initial_strategy(session)
    current = _latest(session)
    if expected_version is not None and current.version != expected_version:
        raise StrategyVersionConflict(expected=expected_version, actual=current.version)
    config = StrategyConfig(
        version=current.version + 1,
        status="PENDING",
        offer_price_markup=markup,
        offer_quantity_fraction=fraction,
        bid_acceptance_policy=bid_acceptance_policy,
        offer_selection_policy=offer_selection_policy,
        span_seconds=span_seconds,
        created_at=datetime.now(timezone.utc),
        created_by=created_by,
    )
    session.add(config)
    session.flush()
    return config


def bind_strategy_to_cycle(session: Session, cycle: Cycle) -> StrategyConfig:
    """Asocia una estrategia una sola vez al ciclo de apertura (AD6).

    La comparación con opensAt oficial impide que una edición efectuada
    a mitad del mercado se aplique al ciclo cuya recepción se retrasó.
    """
    lock_city_ledger(session)
    if cycle.strategy_version is not None:
        existing = session.exec(
            select(StrategyConfig).where(
                StrategyConfig.version == cycle.strategy_version
            )
        ).one()
        return existing

    _ensure_initial_strategy(session)
    opened_at = cycle.market_opens_at or datetime.now(timezone.utc)
    if opened_at.tzinfo is None:
        opened_at = opened_at.replace(tzinfo=timezone.utc)

    # Elegir por la hora de apertura oficial, no por el estado ACTIVE actual:
    # un status-statement retrasado puede pertenecer a un ciclo anterior.
    current = session.exec(
        select(StrategyConfig)
        .where(StrategyConfig.created_at <= opened_at)
        .order_by(StrategyConfig.version.desc())
    ).first()
    if current is None:
        # La configuracion inicial se pudo crear al procesar un status tardio.
        current = session.exec(
            select(StrategyConfig).order_by(StrategyConfig.version)
        ).first()
    if current is None:
        raise RuntimeError("No existe estrategia inicial")

    if current.status == "PENDING":
        active = _latest(session, "ACTIVE")
        # Nunca retroceder el estado global por un ciclo antiguo retrasado.
        if active is None or current.version > active.version:
            for config in session.exec(
                select(StrategyConfig).where(StrategyConfig.status == "ACTIVE")
            ).all():
                config.status = "ARCHIVED"
                session.add(config)
            for config in session.exec(
                select(StrategyConfig).where(
                    StrategyConfig.status == "PENDING",
                    StrategyConfig.version < current.version,
                )
            ).all():
                config.status = "ARCHIVED"
                session.add(config)
            current.status = "ACTIVE"
            current.effective_from_cycle_id = cycle.cycle_id
            session.add(current)

    cycle.strategy_version = current.version
    session.add(cycle)
    session.flush()
    return current


def get_cycle_strategy(session: Session, cycle_id: str) -> Optional[StrategyConfig]:
    """Devuelve la estrategia utilizada realmente; nunca la configuración global."""
    cycle = session.get(Cycle, cycle_id)
    if cycle is None or cycle.strategy_version is None:
        return None
    return session.exec(
        select(StrategyConfig).where(StrategyConfig.version == cycle.strategy_version)
    ).first()
