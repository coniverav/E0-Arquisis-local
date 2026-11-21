# ADR-008: Outbox transaccional y relay confiable

## Contexto y escenario de calidad

Un cambio de negocio en PostgreSQL y una publicación RabbitMQ o un inicio de Step Functions son operaciones sobre sistemas distintos. No existe una transacción única que confirme ambas. E2 persiste mensajes salientes y el connector los publica, pero E3 exige que la intención completa de los mensajes críticos y del inicio de una saga quede en la misma transacción que el cambio que la origina, y que un relay independiente pueda recuperarla.

- **Fuente:** caída del backend o desconexión del broker.
- **Estímulo:** el fallo ocurre entre el `commit` local y la publicación, en un mercado abierto o durante el cierre del ciclo.
- **Ambiente:** una o más réplicas del relay activas.
- **Artefacto:** fila outbox y relay de publicación/inicio de saga.
- **Respuesta esperada:** se transmite exactamente el mensaje almacenado, con el mismo `idpk` y `msgId`; no se pierde ni se inventa una intención y los reintentos son deduplicables por el destinatario.
- **Medida:** cero cambios de estado sin su fila outbox; tras recuperar el servicio, la edad de la fila pendiente más antigua vuelve a cero en un máximo de 60 segundos. Si la recuperación ocurre antes de `validUntil`, el `negotiation-report` debe alcanzar su ventana.

## Alternativas evaluadas

1. **Publicar directamente después del commit.**
   - Es simple y tiene baja latencia en condiciones normales.
   - Una caída entre ambos pasos pierde la publicación, aun cuando el cambio local quedó confirmado.

2. **Outbox transaccional con relay por polling.**
   - Persiste el estado y la intención juntos, permite recuperación tras reinicio y coordina varios relays con bloqueos de filas.
   - Añade consultas periódicas y entrega al menos una vez, por lo que los destinatarios deben deduplicar.

3. **Outbox con `LISTEN/NOTIFY` más polling de respaldo.**
   - Puede reducir la latencia cuando hay actividad.
   - La notificación no es una cola durable y suma conexiones y modos de reconexión sin reemplazar el polling requerido.

4. **Captura de cambios (CDC) sobre PostgreSQL.**
   - Escala como flujo de eventos y reduce consultas de sondeo.
   - Incorpora infraestructura, offsets, retención de WAL y recuperación adicional. No evita por sí misma duplicados y excede el volumen esperado del nodo.

## Decisión

Se implementará una **outbox transaccional en PostgreSQL con relay independiente por polling controlado**. Toda transición de negocio incluida en el alcance escribirá, en la misma transacción, la fila de estado y una fila outbox con el envelope o la solicitud de `StartExecution` completa. Esa fila conservará destino, tipo, `idpk`, `msgId`, correlaciones (`cycleId`, `swapId` cuando existan), payload, `validUntil`, intentos, lease y resultado de publicación.

El relay reclamará lotes con `SELECT FOR UPDATE SKIP LOCKED`, mantendrá un lease recuperable y ejecutará el envío fuera de la transacción que reclama la fila. Sólo marcará una fila como completada después de un publisher confirm de RabbitMQ o de una respuesta válida de `StartExecution`. Si falla entre la aceptación remota y la actualización local, reintentará el mismo registro con las mismas identidades: la garantía es **al menos una vez**, no exactamente una publicación física.

## Mensajes incluidos y excluidos

| Incluidos en el outbox | Motivo |
| --- | --- |
| `negotiation-report` | Su ausencia al cierre tiene costo económico y debe corresponder al estado confirmado del ciclo. |
| `offer`, `offer-updated`, `offer-withdrawn` | La oferta visible debe reflejar la reserva, liberación o modificación persistida. |
| `bid`, `bid-accepted`, `bid-rejected` originados por una decisión local | Son decisiones comerciales que deben sobrevivir a una caída posterior al cambio local. |
| Inicio de saga de comprador o vendedor | La reserva/bid y el intento de `StartExecution` deben recuperarse juntos. |

| Excluidos inicialmente | Justificación |
| --- | --- |
| ACK/NACK de protocolo y confirmación de entrega AMQP | Son respuestas de transporte generadas por el connector, no una transición económica local. Se mantienen sus reglas actuales de reentrega y auditoría. |
| Logs, métricas y heartbeats | No representan un compromiso comercial y no deben bloquear el relay. |
| Publicaciones hechas desde una tarea Lambda ya persistida en una ejecución Standard | Step Functions aporta ejecución durable y reintento de la tarea. La tarea igualmente debe conservar sus identificadores; cualquier excepción deberá documentarse. |

Esta separación no autoriza a publicar directamente una decisión económica nueva desde un componente distinto al relay. Si aparece otro mensaje que refleja un cambio de negocio, se agregará al alcance del outbox antes de usarse en producción.

## Operación y garantías

El relay tendrá métricas de filas pendientes, edad de la más antigua, publicaciones exitosas/fallidas, reintentos y leases vencidos. El feed administrativo podrá usar la outbox como registro ordenado de lo que el nodo decidió emitir. Un runbook deberá indicar cómo verificar una fila por `idpk`, `msgId`, `cycleId` o `swapId`, cuándo esperar la recuperación automática y cuándo escalar.

El diseño conserva los identificadores originales. Una reentrega podrá volver a alcanzar RabbitMQ o Step Functions, pero los consumidores y el nombre de ejecución deduplicarán la operación. No se prometen orden global entre mensajes independientes; para mensajes de una misma entidad el relay deberá respetar una secuencia persistida o bloquear el despacho concurrente de esa clave.

## Consecuencias y tradeoffs

Se intercambia inmediatez por recuperación verificable. Habrá una latencia acotada por el intervalo de polling y trabajo adicional sobre PostgreSQL, pero no se pierde una decisión por una caída entre sistemas. La alternativa de notificación puede reevaluarse con métricas reales si el polling resulta insuficiente; CDC no forma parte de esta entrega.

La validación de E3 debe cortar backend y broker después de un commit, demostrar que el relay recupera las filas sin cambiar sus identificadores y comprobar que el atraso vuelve a cero dentro de 60 segundos.

## Referencias

- [AWS Prescriptive Guidance: Transactional outbox pattern](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html).
- [PostgreSQL: locking clause (`SELECT ... FOR UPDATE SKIP LOCKED`)](https://www.postgresql.org/docs/16/sql-select.html).
