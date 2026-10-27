"""E2-036: costo de transporte de una oferta desde la ciudad compradora.

El contrato v3 expresa distancia en metros y tarifa en CR/(kWh·km).
No confundir precio nominal de la oferta con precio efectivo de compra.
"""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from ..models import DistanceTable

_CENTIMOS = Decimal("0.01")
_KM = Decimal("1000")


def _round2(value: Decimal) -> Decimal:
    return value.quantize(_CENTIMOS, rounding=ROUND_HALF_UP)


def calculate_transport(
    table: DistanceTable | None,
    *,
    seller_city: str,
    price_per_energy: Decimal | None,
) -> dict:
    """Retorna campos nulos si todavía no existe una tarifa verificable.

    Se aplica la tarifa punitiva oficial si la ruta está deshabilitada:
    NO se considera la ruta bloqueada para compras ni se inventa la tarifa.
    """
    empty = dict(
        distanceMeters=None, routeEnabled=None, transportRate=None,
        transportPerEnergy=None, effectivePricePerEnergy=None,
    )
    if table is None:
        return empty
    route = table.distances.get(seller_city)
    if not isinstance(route, dict):
        return empty

    try:
        distance = Decimal(str(route["distance"]))
        enabled = route["enabled"]
        if type(enabled) is not bool:
            return empty
        if not distance.is_finite() or distance < 0:
            return empty
    except (KeyError, ValueError, TypeError, InvalidOperation):
        return empty

    empty.update(distanceMeters=distance, routeEnabled=enabled)
    rate_raw = (
        route.get("transportCost") if enabled
        else table.disabled_route_transport_cost
    )
    if rate_raw is None or price_per_energy is None:
        return empty
    try:
        rate = Decimal(str(rate_raw))
        nominal = Decimal(str(price_per_energy))
        if not (rate.is_finite() and nominal.is_finite()) or rate < 0 or nominal < 0:
            return empty
        per_kwh = _round2(distance / _KM * rate)
        effective = _round2(nominal + per_kwh)
    except (TypeError, ValueError, InvalidOperation):
        return empty
    empty.update(
        transportRate=rate,
        transportPerEnergy=per_kwh,
        effectivePricePerEnergy=effective,
    )
    return empty
