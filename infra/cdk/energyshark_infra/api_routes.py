"""Rutas publicas del API Gateway HTTP de EnergyShark.

La tabla reproduce las 29 rutas del API `energysharkg10-api` inventariado en
`docs/E3-010-inventario-aws.md`. Todas son proxy HTTP al backend del host
productivo y comparten el mismo prefijo `/api`.

`require_auth` marca las rutas protegidas por el autorizador JWT de Auth0.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ApiRoute:
    method: str
    path: str
    require_auth: bool = False

    @property
    def backend_path(self) -> str:
        return f"/api{self.path}"


API_ROUTES: tuple[ApiRoute, ...] = (
    ApiRoute("GET", "/audit/anomalies"),
    ApiRoute("GET", "/audit/intercity", require_auth=True),
    ApiRoute("GET", "/audit/reconciliations", require_auth=True),
    ApiRoute("GET", "/audit/wallet-events", require_auth=True),
    ApiRoute("GET", "/connectivity"),
    ApiRoute("GET", "/cycles"),
    ApiRoute("GET", "/cycles/{cycle_id}"),
    ApiRoute("GET", "/cycles/{cycle_id}/history"),
    ApiRoute("GET", "/distance-table"),
    ApiRoute("GET", "/health"),
    ApiRoute("GET", "/history"),
    ApiRoute("GET", "/history/{event_id}"),
    ApiRoute("GET", "/market/book"),
    ApiRoute("GET", "/market/buy-candidates"),
    ApiRoute("GET", "/market/cycle-metrics"),
    ApiRoute("GET", "/market/cycle-metrics/compare"),
    ApiRoute("GET", "/market/offers"),
    ApiRoute("PATCH", "/market/offers/{offer_id}/price", require_auth=True),
    ApiRoute("POST", "/market/offers/{offer_id}/withdraw", require_auth=True),
    ApiRoute("GET", "/market/strategies"),
    ApiRoute("GET", "/market/strategy"),
    ApiRoute("POST", "/market/strategy", require_auth=True),
    ApiRoute("GET", "/market/strategy/audit", require_auth=True),
    ApiRoute("GET", "/market/strategy/options"),
    ApiRoute("GET", "/market/swaps"),
    ApiRoute("GET", "/market/swaps/{record_id}"),
    ApiRoute("GET", "/negotiations", require_auth=True),
    ApiRoute("POST", "/negotiations", require_auth=True),
    ApiRoute("GET", "/negotiations/{negotiation_id}", require_auth=True),
)
