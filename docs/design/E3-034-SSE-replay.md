# E3-034 — Cursor persistido y replay SSE

El feed administrativo de un nodo se consume con `GET /events/stream`,
`Authorization: Bearer <jwt>` y, al reconectar, `Last-Event-ID`. La respuesta
mantiene los eventos `market`, `wallet`, `saga` y `alert`. Cada frame incluye
`id`, `event` y un JSON con `id`, `type`, `occurredAt`, `cycleId` y `data`
(`source`, `messageType`, `status`). Los envelopes, identificadores de
mensajes, errores internos y secretos quedan fuera.

## Captura y publicación

- Los triggers de E3-032 y E3-033 registran candidatos de auditoría, outbox y
  conciliación en `sse_feed_pending` dentro de la transacción de origen. Un
  rollback descarta sus candidatos. El relay conserva sus propias garantías
  de lease y confirmación. El estado emitido refleja la transición persistida
  del outbox.
- Cada réplica FastAPI inicia un proyector al arrancar. El proyector intenta
  bloquear el cursor con `FOR UPDATE SKIP LOCKED` y mueve hasta 100 candidatos
  visibles en PostgreSQL. Tras un lote completo intenta otro inmediatamente.
  Cuando se vacía la cola, hay contención o falla una consulta, espera 1,5
  segundos antes de reintentar.
- El identificador público sigue el orden de publicación confirmada. Cada lote
  se ordena por `ingest_id` entre los candidatos visibles en su consulta. Se
  vuelve a consultar la cola desde el principio en el siguiente lote. Si una
  transacción tardía confirma un candidato con un `ingest_id` inferior al de
  uno ya proyectado, se añade al final con un ID público nuevo. El
  `occurredAt` registra el momento de captura y puede retroceder dentro del
  orden público.
- Eliminar los candidatos escogidos, insertar los eventos con IDs contiguos y
  avanzar el cursor componen una transacción. Si el proyector cae antes del
  commit, el lote sigue pendiente. Tras el commit, cualquier réplica puede
  continuar desde el cursor confirmado. El proyector usa sólo las tablas SSE.
- El upgrade `e3034ssereplay` agrega la tabla de revocaciones. El cursor,
  los eventos y los candidatos de las revisiones previas siguen disponibles.

## Lectura y recuperación

- Con cursor omitido, vacío o `0` se lee desde el comienzo del historial
  disponible desde E3-032. Con un cursor válido se devuelven exclusivamente
  IDs mayores, en orden ascendente. Cada conexión mantiene su propia posición.
  El cliente debe guardar el ID del último frame completo aplicado y usarlo al
  reconectar. Las reentregas de mensajes de negocio pueden generar hechos de
  auditoría distintos con IDs SSE distintos.
- La lectura toma hasta 100 filas por consulta y comprueba que la página sea
  contigua respecto al cursor y al extremo confirmado en esa misma consulta.
  Una conexión inactiva consulta cada 1,5 segundos y recibe un comentario de
  heartbeat cada 15 segundos sin datos. El heartbeat carece de ID y mantiene
  la posición del cliente.
- Los candidatos se conservan hasta su proyección y el historial publicado se
  conserva indefinidamente en esta versión. El volumen crece con la actividad
  del nodo. Un hueco de almacenamiento devuelve 503 antes de abrir el stream
  o cierra la conexión activa antes de enviar IDs posteriores al hueco.
- Un cursor debe ser decimal ASCII canónico y caber en un `bigint` positivo,
  además del valor inicial `0`. Un formato inválido devuelve 400 y un cursor
  mayor que el último confirmado devuelve 409. Los candidatos pendientes
  todavía carecen de ID público.

## Acceso

- El JWT debe identificar un `sub` y otorgar el scope `read:events` por
  defecto. `SSE_REQUIRED_SCOPE` permite elegir otro scope en el proceso de
  FastAPI. Los operadores autorizados comparten el mismo feed del nodo. El
  cursor sólo indica una posición y no concede acceso a ningún usuario.
- Un token ausente, inválido o vencido devuelve 401. La falta del permiso o
  la revocación del sujeto devuelve 403, antes de comprobar el extremo del
  historial. Al vencer el JWT durante una conexión, el servidor deja de
  emitir y el cliente reconecta con un token vigente y su último cursor.
- Un administrador de la base puede revocar un sujeto registrando su `sub`
  exacto en `sse_feed_revocations.subject`. La revocación bloquea sus tokens
  en todas las réplicas hasta que se retire el registro. La lectura verifica
  la revocación en cada lote y renueva esa verificación al enviar una página
  que tarda más de 1,5 segundos. Un frame ya enviado permanece en el cliente.
  Los cambios de permiso en Auth0 se reflejan al emitir un JWT nuevo. La
  revocación local bloquea también los JWT ya emitidos.
