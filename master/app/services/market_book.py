"""E2-035: proyección de lectura del feed de mercado, sin duplicar persistencia.

El feed E2-017 es la fuente local de snapshots. El cierre explícito llega por
market-closed, y el deadline de la central puede cerrar la vista aun si el
feed perdió ese mensaje (no se deduce desde cycleId).

Un snapshot recibido puede estar desactualizado aunque no sepamos detectarlo.
`uncertain`/`stale` son señales conservadoras, no garantías de frescura.
"""
from datetime import datetime, timezone

from sqlmodel import Session, select

from ..models import Cycle, DistanceTable, MarketCycle, MarketFeedOffer
from .transport_pricing import calculate_transport
from ..schemas import MarketBookOfferOut, MarketBookOut


def get_market_book(
    session: Session, *, cycle_id: str, include_inactive: bool = False,
    now: datetime | None = None,
) -> MarketBookOut:
    now = now or datetime.now(timezone.utc)
    market = session.get(MarketCycle, cycle_id)
    cycle = session.get(Cycle, cycle_id)
    closes_at = cycle.market_closes_at if cycle else None
    market_closed_at = market.closed_at if market else None
    # La central define el deadline. El reloj local solamente permite comparar
    # contra ese instante; una deriva de reloj no modifica el estado persistido.
    closed = (
        market_closed_at is not None
        or (closes_at is not None and now >= closes_at)
    )

    # Evita aplicar accidentalmente al ciclo actual una tabla de otro ciclo.
    # Como la central puede actualizarla varias veces, se usa la más reciente.
    distance_table = session.exec(
        select(DistanceTable)
        .where(DistanceTable.cycle_id == cycle_id)
        .order_by(
            DistanceTable.source_timestamp.desc(),
            DistanceTable.received_at.desc(),
            DistanceTable.id.desc(),
        )
    ).first()

    snapshots = session.exec(
        select(MarketFeedOffer).where(MarketFeedOffer.cycle_id == cycle_id)
    ).all()

    items: list[MarketBookOfferOut] = []
    for snapshot in snapshots:
        if not include_inactive:
            if (closed or snapshot.state != "ACTIVE"
                    or snapshot.remaining is None or snapshot.remaining <= 0
                    or snapshot.price_per_energy is None):
                continue
        transport = calculate_transport(
            distance_table,
            seller_city=snapshot.city_id,
            price_per_energy=snapshot.price_per_energy,
        )
        items.append(MarketBookOfferOut(
            **transport,
            cycleId=snapshot.cycle_id,
            cityId=snapshot.city_id,
            offerId=snapshot.offer_id,
            quantity=snapshot.initial_quantity,
            remaining=snapshot.remaining,
            pricePerEnergy=snapshot.price_per_energy,
            lastEventAt=snapshot.last_event_at,
            lastEventMsgId=snapshot.last_event_msg_id,
            state=snapshot.state,
            uncertain=snapshot.uncertain,
            stale=(snapshot.uncertain or snapshot.state == "UNRESOLVED" or closed),
        ))

    # Orden estable: menor precio nominal, ciudad, offerId.
    # El precio efectivo (transporte) corresponde a E2-036, no a esta tarea.
    items.sort(key=lambda item: (
        item.pricePerEnergy is None,
        item.pricePerEnergy if item.pricePerEnergy is not None else 0,
        item.cityId, item.offerId,
    ))

    return MarketBookOut(
        cycleId=cycle_id, asOf=now, marketClosed=closed,
        marketClosedAt=market_closed_at, closesAt=closes_at,
        distanceTableTimestamp=(distance_table.source_timestamp if distance_table else None),
        disabledRouteTransportCost=(
            distance_table.disabled_route_transport_cost if distance_table else None
        ),
        total=len(items), offers=items,
    )
