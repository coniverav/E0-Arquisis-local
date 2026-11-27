# E3-054 · Transiciones de saga, alertas y correlación

El historial de KLD conserva los IDs emitidos por E3-034. La migración aditiva captura los hechos nuevos desde su instalación. Al insertar una saga o cambiar realmente su estado, un trigger registra una fila en `sse_feed_pending` dentro de la misma transacción. Un rollback descarta ambas escrituras. El trigger evita tomar el bloqueo del cursor público. El proyector existente asigna los IDs definitivos después del commit, junto con los demás eventos del nodo. La fecha de captura puede retroceder respecto del orden de publicación.

Las tramas `saga-transition` reflejan el estado persistido en `saga_executions`: `PENDING_START`, `RUNNING`, `WAITING`, `SETTLED`, `REJECTED`, `FAILED`, `COMPENSATED` o `TIMED_OUT`. El `STARTED` previo sigue identificando la confirmación de despacho de `SAGA_START` en el outbox. Una escritura que conserva el mismo estado no agrega otra transición.

## Fuentes de alerta

| Familia (`data.messageType`) | Evidencia para publicar | Identidad para evitar duplicados |
| --- | --- | --- |
| `ledger-wallet-delta` | Inserción de una conciliación con estado real `DIVERGENT`. Las conciliaciones `NO_LOCAL_BASELINE` y `EXPLAINED` siguen siendo eventos Wallet. | Fila de conciliación en su transacción |
| `saga-stuck` | Saga local abierta cuyo ciclo lleva más de 60 s cerrado según `market_closes_at` (AD7). | Primera observación por ejecución |
| `outbox-aged` | Fila despachable en `PENDING`, `CLAIMED` o `FAILED` que supera el umbral configurado desde `created_at`. | Primera observación por fila |
| `wallet-slow` | Apertura de incidente en una condición verificada de latencia Wallet de la cuenta y nodo seleccionados. | Cuenta, condición e incidente |
| `consumer-lag` | Apertura de incidente en una condición verificada de atraso o pérdida de señal de un consumidor del nodo. | Cuenta, condición e incidente |

El observador local consulta PostgreSQL cada 15 s en transacciones independientes. Examina hasta 100 observaciones nuevas por familia y vuelta. Cada observación y su candidato se confirman juntos. Los eventos `OPEN` registran una apertura histórica. Para determinar el estado actual hay que consultar la fuente. Una fila existente al instalar la migración puede producir una alerta de edad si todavía cumple la condición cuando se observa.

`SSE_OUTBOX_AGE_SECONDS` es opcional y debe contener el umbral positivo aprobado para el ambiente, idéntico en ambos Master. Sin valor se omite la familia de edad de outbox. El documento E3-037 propone 30 s para evaluación. El límite de 60 s de AD8 mide recuperación del backlog y tiene otra semántica. [Contrato de métricas E3-037](../observability/E3-037-base-metrics.md)

## Incidentes externos

El importador lee `NrAiIncident` por cuenta, IDs de condición y ventana. Su identidad de lectura de NerdGraph se proporciona al proceso en `NEW_RELIC_USER_API_KEY`. No se envían descripciones, IDs de incidente ni credenciales al feed. El servicio usa el endpoint estadounidense de NerdGraph, igual que la herramienta E2-061. [Atributos oficiales de incidentes](https://docs.newrelic.com/docs/alerts/create-alert/condition-details/alert-event-attributes/)

Tras verificar en staging las condiciones de E3-063/064 para KLD y sus umbrales, el operador puede programar esta invocación desde `master/` con acceso a la base de ese ambiente. Cada parámetro de condición es opcional y requiere un ID real distinto. La ventana predeterminada cubre la hora anterior. Usar ventanas solapadas para tolerar retrasos de ingesta. Después de una parada más larga, repetir las ventanas omitidas con `--since-ms` y `--until-ms` (Unix en milisegundos). Si la consulta alcanza 1000 resultados, el importador solicita dividir la ventana antes de publicar.

```bash
timeout 60 python -m app.workers.sse_incidents \
  --account "$NR_ACCOUNT_ID" \
  --wallet-condition "$NR_WALLET_LATENCY_CONDITION" \
  --consumer-condition "$NR_CONSUMER_LAG_CONDITION"
```

El repositorio contiene emisores de métricas de latencia y heartbeat. La activación requiere identificar condiciones reales, fijar el umbral Wallet con el p95 de referencia y programar el importador. La ausencia de mensajes en una cola o una respuesta HTTP 429 por sí solas no demuestran las condiciones anteriores.

## Consulta por correlación

Enviar `POST /events/search` al host HTTPS del feed, con el mismo bearer que el stream y `Content-Type: application/json`. El cuerpo contiene exactamente un selector de coincidencia completa (`cycleId`, `swapId` o `idpk`) y opcionalmente `after`, una cadena decimal con el último ID procesado. Ejemplo de cuerpo:

```json
{"cycleId":"ciclo-de-prueba","after":"0"}
```

La respuesta contiene `events`, en orden ascendente, con hasta 100 eventos en el mismo formato público y con los mismos IDs del stream. Para avanzar, usar el último ID devuelto. La búsqueda por swap o clave utiliza la correlación en las filas fuente conservadas de saga, auditoría y outbox. Así puede encontrar transiciones anteriores a la asignación de un swap. Los incidentes de infraestructura sin correlación de negocio carecen de ciclo y swap.

Un JWT válido con sujeto y permiso SSE habilita el feed compartido del nodo. Cada petición comprueba la revocación del sujeto en la misma instantánea que la búsqueda. Un cursor o un identificador conocido no concede acceso. La búsqueda recibe el selector en el cuerpo, evita reflejarlo en respuestas y devuelve `Cache-Control: no-store`. Usar el log dedicado de Nginx y mantener desactivada la captura de cuerpos o parámetros SQL sensibles en proxies y APM.

## Coordinación de publicación

Para Contracts y frontend, los nuevos valores de `data.source` son `SAGA` (fila de ejecución) y `MONITORING` (incidente externo). El panel E3-052 filtra fuentes mediante una lista cerrada. Incorporar ambos valores y la ruta de búsqueda al contrato compartido y al cliente antes de activar la migración en un ambiente con usuarios. Un cliente antiguo puede descartar esos eventos y avanzar su checkpoint al siguiente evento que reconozca.

La migración crea un índice de procedencia en el historial y actualiza restricciones SQL. Planificar su aplicación según el volumen de la tabla. El rollback del código requiere conservar el historial y los IDs ya emitidos. La validación de incidencias SaaS, la programación del importador y el recorrido navegador–proxy desplegado requieren el ambiente correspondiente.

## Validación local

Desde `master/`, usar PostgreSQL UTF-8 desechable y una `DATABASE_URL` que apunte a una base exclusiva `energyshark_test_*`. La fixture migra esa base y limpia sus tablas entre casos. La regresión HTTPS requiere Nginx, curl y OpenSSL disponibles en PATH.

```bash
timeout 360 .venv/bin/python -m pytest -q \
  tests/test_e3054_sse.py tests/test_e3032_e3033_sse.py tests/test_e3053_sse_https.py \
  tests/test_auth.py tests/test_e3026_outbox_db.py tests/test_e3036_outbox_faults.py \
  tests/test_e3016_saga_migration.py tests/test_e3015_saga_tokens.py \
  tests/test_wallet_reconciliation_e2044.py tests/test_e3048_saga_recovery_db.py \
  tests/test_e3047_buyer_terminal_db.py tests/test_e3042_seller_terminal_db.py
```

Los casos de búsqueda usan JWT firmados de prueba, dos sujetos con permisos distintos y una revocación persistida. El caso HTTPS verifica el log real del proxy local. Los incidentes SaaS se comprueban con transporte HTTP simulado, incluyendo ventanas solapadas, respuestas incompletas y condiciones ajenas.
