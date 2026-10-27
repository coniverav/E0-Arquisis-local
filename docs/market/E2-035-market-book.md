# E2-035 · Libro de ofertas vigente (backend)

## Fuente de datos

Usa la proyección `market_feed_offers` generada por E2-017, no `market_offers`
(modelo de oferta propia / reservaciones de AD4). El historial original
continúa en `inbound_messages`; las escrituras se conservan como estaban.

`GET /market/book?cycleId=cycle-9431` devuelve únicamente snapshots `ACTIVE`
con `remaining > 0`, precio presente y mercado abierto. Ordena por precio
**nominal**, no por precio efectivo de transporte. No infiere ciclo vigente
a partir del ID ni selecciona automáticamente uno.

Con `includeInactive=true` también devuelve estados `WITHDRAWN`, `CLOSED` o
`UNRESOLVED`, con `uncertain`, `stale`, `lastEventAt` y `lastEventMsgId` para
explicar la frescura del feed. `uncertain` refleja ordenamientos conflictivos
que E2-017 conoce, **no certifica** que no se perdieron mensajes.

El cierre proviene de `market-closed` o de la comparación con el `closesAt`
recibido de la central en el `status-statement`. La hora local no inventa
ventanas, solo compara contra un timestamp oficial persistido.

## Responsabilidades de las tarjetas siguientes

- E2-036: distancia de comprador, tarifa punitiva y precio efectivo con round2.
- E2-037: estrategia AD6 y candidatas respetando `minBid` y el tiempo oficial.
- E2-038: enqueue de bids por outbox, idpk y msgId durables, ACK independiente.
- E2-039: recibir respuestas, correlacionar por target, deduplicar por idpk.

El endpoint **no reserva energía ni crea bids ni hace llamadas a wallet**.
El `remaining` del feed sigue siendo potencialmente obsoleto.
