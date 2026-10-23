# Post-mortem AD1: Topología del consumo del broker

### Contexto y decisión de E1

AD1 buscaba consumir mensajes RabbitMQ sin acoplar el ciclo de vida del consumidor a la API, recuperarse de cortes y registrar las recepciones antes de confirmar su entrega al broker. Se eligió **un connector independiente que se comunica por HTTP con FastAPI**, con persistencia de recepción y de publicaciones pendientes en PostgreSQL. Se descartó integrar el consumidor dentro de las réplicas de `master` por el acoplamiento y la coordinación adicional que habría requerido.

### Qué ocurrió al implementar E1

El código E1 tiene un `connector` separado que reintenta la conexión y envía por HTTP los mensajes válidos a `master` (`E0-Arquisis-local/connector/connector.py:405-620`). `record_inbound_message` confirma la recepción en PostgreSQL antes del efecto de negocio; los descartes y NACK tienen un camino de auditoría propio (`E0-Arquisis-local/master/app/services/message_audit.py:21-55`, `E0-Arquisis-local/master/app/routers/message_audit.py:348-378`). El outbox persiste propuestas y reportes, entre otros mensajes, para su despacho; ACK/NACK se publican en el camino inmediato tras registrar la intención. Ambos Compose **configuran** dos réplicas de API y un connector; esto describe el checkout, no prueba disponibilidad o recuperación en producción.

### Qué cambió con el protocolo v3

La arquitectura de una sola cola ya no basta. En E2 el nodo debe consumir dos colas con comportamientos diferentes:

- `city.{CODE}`: canal dirigido de central, otras ciudades y wallet. Mantiene validación de envelope y respuestas ACK/NACK; para mensajes de otras ciudades se verifica además que `user_id` corresponda a `cityId`. En el checkout E1, la cola concreta de KLD se configura como `city.KLD.q`.
- `market.{CODE}`: feed de `offer`, `offer-updated`, `offer-withdrawn` y `market-closed`. No corresponde enviar ACK de protocolo a estos mensajes. La cola tiene TTL de un ciclo y máximo 5.000 mensajes, por lo que debe drenarse oportunamente.

Se detectan incompatibilidades concretas en el checkout E1: `consume_forever` abre solo `RABBITMQ_QUEUE`; `complete_successful_message` y el camino NACK publican en `RABBITMQ_CENTRAL_ROUTING_KEY`, aunque el mensaje provenga de otra ciudad (`E0-Arquisis-local/connector/connector.py:233-273,405-450,500-564`). Además, `E0-Arquisis-local/connector/protocol/validation.py:10-49` no reconoce los tipos v3. Estas limitaciones afectan **G02, RF02, RF03 y RF05**.

### Reevaluación de la decisión

**Se mantiene como propuesta arquitectónica la separación entre connector y API** y se reutilizan los mecanismos de auditoría y outbox de E1.

**Lo que se debe modificar:** ampliar el connector para consumir ambas colas, separar sus políticas de procesamiento, direccionar las respuestas al emisor correcto en el canal dirigido, incorporar los contratos v3 y observar el estado del feed y sus posibles pérdidas. La lógica económica seguirá estando en el backend; connector conservará su responsabilidad de transporte y validación de protocolo.

### Tradeoff y lecciones

La separación de responsabilidades sigue siendo adecuada para E2, pero la implementación E1 quedó acoplada a una sola cola y a respuestas dirigidas a la central. Extenderla exige nuevas rutas, estados, validaciones y pruebas de recuperación; el código actual todavía no demuestra que pueda drenar el feed bajo carga o tras un corte.

### Acciones y evidencia pendientes

- Probar recepción simultánea de ambas colas y publicación de ofertas en `market.offer.{CODE}`.
- Verificar ACK/NACK hacia otras ciudades, incluido `IDENTITY_MISMATCH`, y ausencia de ACK de protocolo en el feed de mercado.
- Demostrar deduplicación, reconexión y persistencia de los caminos de recepción y publicación ante cortes, incluyendo fallos entre el commit y el ACK al broker.
- Verificar con pruebas y en producción las nuevas métricas de consumo de mercado y los efectos de TTL/límites de cola.

### Conclusión

**AD1 sigue siendo una base de diseño para v3, pero su extensión está pendiente.** El checkout conserva connector independiente, auditoría y outbox E1; debe corregir los supuestos de una cola y de respuestas dirigidas siempre a la central.
