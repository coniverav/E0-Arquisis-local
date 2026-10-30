# E2-072 — Frescura y pérdida potencial del feed de mercado

El broker del curso administra `market.KLD`: TTL de un ciclo y capacidad de 5.000 mensajes. El nodo consume la cola sin declarar ni modificar topología. El protocolo v3.0.2 no define secuencia global, cursor ni `request` para reproducir ofertas; las únicas consultas `request/ask` válidas son `status-statement` y `distance-table`.

## Comportamiento local

- El Connector conserva conexión propia, QoS y ACK de entrega sólo después de la persistencia. Tras **cada conexión o reconexión**, avisa a Master antes de reanudar el consumo. Si Master no confirma el aviso, reintenta la conexión y no empieza a consumir.
- Master registra `feed_gap_at` para los ciclos oficialmente abiertos. En `GET /market/book`, una oferta cuyo último evento antecede a esa marca se muestra `stale: true` y no entra en las candidatas de compra. Un `offer-updated` posterior puede volver a mostrar su estado actual; un evento faltante de otra ciudad no puede reconstruirse.
- La respuesta del libro incluye `feedGapAt`, `lastFeedEventAt` y `freshness`: `OBSERVED`, `NO_FEED`, `NO_STATUS`, `GAP_OBSERVED`, `CLOSED_BY_DEADLINE` o `CLOSED_CONFIRMED`. `market-closed` persiste el cierre; si se perdió, el plazo `closesAt` del `status-statement` cierra el libro de forma conservadora sin fingir recepción del mensaje.
- Tras la conexión se encolan solicitudes idempotentes de `status-statement` y `distance-table` para recuperar **ese contexto**, dentro del cupo existente de `request/ask`. No se solicita replay de ofertas porque el contrato no lo permite.
- La observabilidad del Connector registra `sourceAgeSeconds` para entregas de mercado con timestamp válido. Es una aproximación a atraso de origen y cola; no es la edad interna RabbitMQ ni una prueba de descarte por capacidad.

## Límite de garantía

El broker puede descartar mensajes antiguos por TTL o capacidad **sin avisar al consumidor**. Una conexión continua y un feed silencioso no prueban completitud; el nodo no puede detectar todas las pérdidas ni reconstruir ofertas de terceros a partir del protocolo disponible. La marca de brecha expresa incertidumbre observada por reconexión. Para verificar backlog, TTL efectivo y eventuales descartes se requieren métricas del broker o una capacidad oficial de replay que el contrato actual no ofrece. No se declara recuperación completa en este caso.
