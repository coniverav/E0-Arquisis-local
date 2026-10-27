
"""AD6: activación de estrategias y asociación fija por ciclo."""

from datetime import datetime, timezone

from sqlmodel import Session, select

from app.models import Cycle, StrategyConfig
from app.services.budget_carryover import lock_city_ledger


# Importante: esta función no hace commit. 
# Así, si falla el procesamiento del status-statement, también se revierte el cambio de estrategia.
def bind_strategy_to_cycle(
    session: Session,
    cycle: Cycle,
) -> StrategyConfig:
    """
    Fija una única versión al recibir el status-statement.

    - Una versión PENDING se activa en el nuevo ciclo.
    - La versión ACTIVE anterior pasa a ARCHIVED.
    - Los ciclos anteriores conservan su versión.
    - Si no existe ninguna estrategia, crea la versión inicial.

    No realiza commit: participa de la transacción del mensaje.
    """

    # Coordina master, master2 y demás procesos.
    lock_city_ledger(session)

    # Nunca cambiar una versión ya asignada.
    if cycle.strategy_version is not None:
        return session.exec(
            select(StrategyConfig).where(
                StrategyConfig.version == cycle.strategy_version
            )
        ).one()

    pending = session.exec(
        select(StrategyConfig)
        .where(StrategyConfig.status == "PENDING")
        .order_by(StrategyConfig.version.desc())
    ).first()

    active = session.exec(
        select(StrategyConfig)
        .where(StrategyConfig.status == "ACTIVE")
        .order_by(StrategyConfig.version.desc())
    ).first()

    if pending is not None:
        selected = pending

        # Archivar la versión activa anterior y las pendientes
        # que quedaron reemplazadas por una versión más reciente.
        previous = session.exec(
            select(StrategyConfig).where(
                StrategyConfig.status.in_(["ACTIVE", "PENDING"])
            )
        ).all()

        for strategy in previous:
            if strategy.version != selected.version:
                strategy.status = "ARCHIVED"
                session.add(strategy)

        selected.status = "ACTIVE"
        selected.effective_from_cycle_id = cycle.cycle_id

    elif active is not None:
        # Reutilizar la vigente si no hay modificaciones pendientes.
        selected = active

        # Normalizar posibles ACTIVE duplicadas de datos antiguos.
        other_active = session.exec(
            select(StrategyConfig).where(
                StrategyConfig.status == "ACTIVE",
                StrategyConfig.version != selected.version,
            )
        ).all()

        for strategy in other_active:
            strategy.status = "ARCHIVED"
            session.add(strategy)

    else:
        # Inicialización del sistema.
        latest_version = session.exec(
            select(StrategyConfig.version)
            .order_by(StrategyConfig.version.desc())
        ).first() or 0

        selected = StrategyConfig(
            version=latest_version + 1,
            status="ACTIVE",
            offer_price_markup="1.20",
            offer_quantity_fraction="1.00",
            bid_acceptance_policy="FIFO",
            offer_selection_policy="CHEAPEST_EFFECTIVE_PRICE",
            span_seconds=60,
            effective_from_cycle_id=cycle.cycle_id,
            created_at=datetime.now(timezone.utc),
            created_by="system",
        )

    session.add(selected)
    session.flush()

    cycle.strategy_version = selected.version
    session.add(cycle)
    session.flush()

    return selected
