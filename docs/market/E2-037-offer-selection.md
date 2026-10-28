# E2-037 — Selección de ofertas para comprar (RF03, AD6)

`GET /market/buy-candidates?cycleId=...` ofrece una **vista de solo lectura**:
no escribe en PostgreSQL, no reserva créditos, no llama a la wallet y no envía bids.
La ejecución durable del bid se implementa en E2-038.

- Se usa la **estrategia versionada asociada al ciclo** por AD6, nunca la versión global pendiente.
- `minBid` se lee de `status-statement.data.market.minBid`, no de cada oferta. No hay valor predeterminado si falta.
- El tiempo oficial proviene de `opensAt`, `lastBidAt` y `closesAt` del ciclo. La ventana para **considerar comprar** comienza en `opensAt + spanSeconds` y termina antes de `lastBidAt` y `closesAt`. El servicio no infiere estos valores.
- Se excluyen ofertas propias, retiradas, `uncertain/stale`, sin `remaining >= minBid` y sin ruta o precio efectivo calculable.
- Una ruta con `enabled=false` **no se excluye** si E2-036 pudo calcular su tarifa punitiva oficial.
- `CHEAPEST_EFFECTIVE_PRICE` ordena ascendentemente por precio efectivo y distancia. `MIN_DISTANCE` ordena por distancia y luego precio efectivo; ambos tienen desempates deterministas.
- `canBid` describe solo **elegibilidad temporal del resultado**, no disponibilidad acreditada en la wallet ni confirmación de una compra. Los snapshots `remaining` del feed pueden ser obsoletos.

Al generar bids, E2-038 debe **revalidar** la ventana y disponibilidad y conservar `idpk` en reintentos. E2-039 debe correlacionar respuestas sin duplicar efectos.

## Validación

`pytest -q tests/test_offer_selection.py tests/test_market_book.py tests/test_transport_pricing.py`

Las pruebas usan PostgreSQL local aislado, no RabbitMQ ni wallet productivos.
