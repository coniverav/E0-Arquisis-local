# Post-mortem AD1: Topología del consumo del broker (E2-082)

**Revisión retrospectiva:** 3 de noviembre de 2026. Se examina el estado de E1 y su adaptación en el código de E2 del repositorio recibido. Las decisiones originales de [AD1](../ADR-001-broker-topology.md) no se reescriben.

### Decisión y resultado de E1

AD1 eligió un `connector` independiente que transporta mensajes RabbitMQ a FastAPI mediante HTTP. El backend persiste recepción, estados de negocio y publicaciones pendientes en PostgreSQL antes de devolver la confirmación que permite completar el ACK de entrega al broker. El diseño evitó incorporar consumidores a cada réplica de `master`, con el costo de un salto HTTP y un inbox/outbox que debe poder recuperarse. En E1 se atendía principalmente una cola dirigida, por lo que el supuesto de transporte único dejó de representar los dos flujos de E2.

### Cambio real al protocolo v3

E2 requiere separar `city.{CODE}` y `market.{CODE}`. El primero lleva mensajes dirigidos, valida el envelope y puede producir respuestas ACK/NACK de protocolo en la ruta del emisor autenticado. El segundo entrega eventos de mercado y **no** requiere ACK/NACK de protocolo; conserva solo el ACK de **entrega RabbitMQ** después de persistir el evento. La central administra TTL y capacidad de la cola market, por lo que el consumo continuo no prueba que nunca se pierdan eventos.

[`connector/connector.py`](../../../connector/connector.py) ejecuta `consume_forever` y `consume_market_forever` como tareas independientes, distingue `process_market_message` y `market_consumer_heartbeat`, y reintenta conexiones. [`connector/protocol/`](../../../connector/protocol/) contiene la validación y el direccionamiento del canal dirigido. El backend persiste eventos y snapshots de market mediante [`market_feed.py`](../../../master/app/services/market_feed.py); la recepción dirigida y el outbox se apoyan en [`message_audit.py`](../../../master/app/services/message_audit.py) y las rutas internas. [`E2-072`](../../market/E2-072-feed-freshness.md) añade una marca de brecha observada tras conexión/reconexión, invalida ofertas antiguas para nuevas compras y recupera solo `status-statement` y `distance-table`, no un replay inexistente de ofertas.

### Evidencia disponible

- [`connector/tests/test_market_consumer.py`](../../../connector/tests/test_market_consumer.py): ACK RabbitMQ posterior a persistencia, ausencia de ACK de protocolo en market, descarte auditado y reconexión simulada.
- [`connector/tests/test_directed_ack_nack.py`](../../../connector/tests/test_directed_ack_nack.py): respuesta dirigida a ciudad/central según emisor, identidad inválida y no respuesta a mensajes de control.
- [`master/tests/test_market_feed_persistence.py`](../../../master/tests/test_market_feed_persistence.py): historia y último estado del feed, deduplicación, rollback y concurrencia de sesiones.
- **Observación operativa aportada durante E2-061 (31/10/2026):** logs de EC2 con conexiones `city` y `market`, `broker_heartbeat consumer=market` y `broker_delivery ... broker_ack`. Es evidencia de operación y telemetría, **no** un ensayo de desconexión prolongada, TTL agotado o mensajes perdidos. Esos logs no se incluyen como archivo versionado en este ZIP.

Los archivos de pruebas acreditan cobertura diseñada, no una ejecución reciente de toda la suite en el paquete. Tampoco se adjuntan métricas del broker que demuestren consumo sin descartes bajo carga.

### Tradeoff y divergencias

**La decisión de separar connector y API se mantiene.** El cambio a dos colas se resolvió separando políticas de consumo dentro del mismo servicio, en lugar de introducir consumidores en `master`. Se agregaron tareas, caminos de auditoría y señales de frescura, a cambio de conservar una responsabilidad de transporte clara.

La divergencia relevante respecto de E1 es que una sola cola dirigida no permite expresar el feed efímero. Persistir antes del ACK resuelve el problema local de recepción, pero **no recupera eventos ya descartados por TTL o capacidad en RabbitMQ**. La marca de brecha tampoco detecta pérdidas silenciosas durante una conexión continua.

### Cierre y acciones pendientes

AD1 queda **conservada y extendida en el código E2**. Para evaluar completamente la resiliencia faltan ensayos de caída entre commit/ACK, corte real de RabbitMQ, recuperación con ambas colas y observabilidad del backlog o descartes del broker. No corresponde afirmar que la ausencia de pérdidas esté demostrada.
