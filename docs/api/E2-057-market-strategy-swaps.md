# E2-057 — Endpoints de mercado, estrategias y swaps (RF02/RF04)

Se reutiliza `GET /market/book?cycleId=...` de E2-035/036: sus ofertas ya incluyen
`pricePerEnergy`, `remaining`, `distanceMeters`, `transportPerEnergy`,
`effectivePricePerEnergy`, `routeEnabled`, `uncertain` y `stale`. No consultar
`MarketOffer` como reemplazo del snapshot del feed recibido.

## Consultas (solo lectura)

- `GET /market/book?cycleId=...&includeInactive=false` — mercado recibido y tarifa local de transporte.
- `GET /market/buy-candidates?cycleId=...` — candidatas ordenadas según estrategia efectiva, sin enviar bids.
- `GET /market/offers?cycleId=...&offset=0&limit=50` — ofertas **locales** de venta, incluidas sus cantidades restantes.
- `GET /market/strategy?cycleId=...` — estrategia `active`, última `pending` y `cycleStrategy` efectiva (si se indica un ciclo existente).
- `GET /market/strategies` — historial versionado ordenado por versión descendente.
- `GET /market/swaps?cycleId=...&status=...&role=...&offset=0&limit=50` — swaps paginados, con estados y total filtrado.
- `GET /market/swaps/{record_id}` — detalle por identificador **interno** único.

Las consultas de swaps muestran `proposalHttpAcceptedAt` y estados durables.
HTTP 202 no equivale a `SETTLED`. La proyección **nunca expone**
`swap_token`, `idempotency_key`, `accept_idempotency_key` ni JWT de la wallet.

## Escritura autenticada

`POST /market/strategy` requiere bearer JWT validado mediante `verify_jwt` y recibe:

```json
{
  "offerPriceMarkup": "1.17",
  "offerQuantityFraction": "0.8",
  "bidAcceptancePolicy": "FIFO",
  "offerSelectionPolicy": "MIN_DISTANCE",
  "spanSeconds": 45
}
```

Devuelve 201 con la nueva versión `PENDING`. Reutiliza
`create_pending_strategy` (E2-026/AD6): no cambia la estrategia de ciclos ya
abiertos; la siguiente apertura efectiva la activa según las reglas AD6.
La edición seller existente (`PATCH /market/offers/{id}/price`,
`POST /market/offers/{id}/withdraw`) conserva su autenticación.

El backend no escribe swaps mediante endpoints públicos: las operaciones
wallet/connector continúan a cargo de workers idempotentes (AD5).

## Pruebas

`python -m pytest -q tests/test_e2057_market_api.py tests/test_market_book.py tests/test_transport_pricing.py tests/test_strategy_config_ad6.py tests/test_auth.py --tb=short`

Usar `DATABASE_URL` de una PostgreSQL exclusiva cuyo nombre empiece con
`energyshark_test_` (restricción del `conftest.py`).
