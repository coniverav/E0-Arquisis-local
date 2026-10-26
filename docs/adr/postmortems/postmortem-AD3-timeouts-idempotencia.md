# Post-mortem AD3: Timeouts e idempotencia de operaciones

### Contexto y decisión de E1

AD3 trataba las negociaciones asíncronas con la central, sus plazos de 30 segundos para propuesta y, en el caso `give`, pago, y la necesidad de reintentar sin duplicar efectos. Se eligieron **deadlines y estados persistidos en PostgreSQL, gestionados fuera de las requests HTTP**, en lugar de esperar dentro de los endpoints o depender de timers en la memoria de cada `master`.

En la implementación, el reintento de `negotiation-proposal` tras timeout y el reintento de `negotiation-report` tras `REPORT_TOO_EARLY` conservan el `idpk` y generan un nuevo `msgId` (`E0-Arquisis-local/master/app/services/negotiation_proposals.py:229-320`, `E0-Arquisis-local/master/app/services/negotiation_report_dispatch.py:96-154`). Un reintento de publicación del mismo mensaje conserva el payload del outbox; una corrección de reporte usa un `idpk` nuevo. El mecanismo buscaba soportar reinicios y dos instancias de API sin ejecutar dos veces el mismo efecto contable.

### Qué ocurrió al implementar E1

`E0-Arquisis-local/master/app/models.py:232-328` persiste estados/deadlines de negociaciones y reportes; `E0-Arquisis-local/master/app/workers/cycle_scheduler.py:33-108` procesa ventanas, vencimientos y reintentos. `E0-Arquisis-local/master/app/services/idempotency.py:18-42` reclama el `idpk` en la transacción del efecto. Las pruebas `test_negotiation_timeouts.py`, `test_negotiation_retries.py` y `test_e1_v2_reports.py` cubren casos E1; no demuestran todavía recuperación ante cortes reales de broker o wallet.

### Qué cambió con el protocolo v3

El flujo de E1 `negotiation-proposal → give/take → transfer de pago` fue **retirado**. Por tanto, sus timeouts de 30 segundos ya no deben gobernar el mercado de E2. **`negotiation-report` permanece vigente**, con ventana y reintentos propios; no debe confundirse con la negociación retirada.

Ahora los tiempos y reintentos corresponden a ofertas, bids, swaps y wallet:

- `POST /swaps`, `accept` y `cancel` requieren **`Idempotency-Key` estable** por operación.
- Un HTTP **202 confirma recepción, no ejecución**; el resultado real lo determinan eventos de wallet o consultas de estado.
- Una respuesta HTTP perdida o ambigua puede ocultar una operación procesada: se debe recuperar/reintentar con la **misma llave**, no crear otra.
- `expiresAt` del swap puede fijarse hasta 60 segundos desde el momento de solicitarlo; la wallet determina su expiración. Los hitos del mercado (`lastOfferAt`, `lastBidAt`, `closesAt`, `reportOpensAt`) vienen del `status-statement` de la central.
- Un `bid` repetido con el mismo `idpk` debe conservar su resultado lógico y no proponer un segundo swap.


### Reevaluación de la decisión

**Se conserva como principio el estado durable, los reintentos idempotentes y el procesamiento diferido fuera de las requests HTTP.** El worker y el outbox E1 son una base, no una implementación de seguimiento de swaps ni de escritura a wallet.

**Se debe cambiar la máquina de estados y el criterio de finalización:** los plazos locales de seguimiento se relacionarán con bids, marcas del ciclo y vencimiento informado por wallet, sin atribuir al worker local autoridad para expirar swaps. La operación no termina porque un POST responda 202; se debe procesar el evento correspondiente o consultar su estado. El reporte E1 conserva su propio camino de cierre y correcciones.

La disciplina de AD3 puede reutilizarse en el mercado (RF03), la serialización de aceptaciones (AD4) y la aplicación idempotente de eventos (AD5); esas adaptaciones siguen pendientes. **No se reutiliza literalmente el flujo de negocio retirado.**

### Tradeoff y lecciones

Persistir estados y vencimientos sigue siendo más complejo que usar un timer en memoria, pero permite recuperar trabajo pendiente ante reinicios y réplicas múltiples. E2 agrega la correlación del `idpk` de cada mensaje dirigido, la `Idempotency-Key` de cada escritura wallet y los eventos asociados al swap. El `msgId` identifica el mensaje concreto; no se debe asumir que el `idpk` de un bid sea automáticamente la llave HTTP para proponer o aceptar el swap.

### Acciones y evidencia pendientes

- Desactivar el flujo E1 retirado para E2, manteniendo el historial antiguo consultable.
- Modelar estados y plazos locales de seguimiento de bids/swaps de forma persistente, usando las marcas de tiempo centrales y el `expiresAt` de wallet.
- Persistir llaves de idempotencia para escrituras wallet y correlacionar eventos posteriores; respetar `Retry-After` en 429 y diferenciar errores terminales de respuestas inciertas.
- Probar timeout con operación procesada pero HTTP 202 perdido; verificar que la recuperación converge sin nuevo swap ni doble asiento.
- Probar reinicio de worker/API con operaciones pendientes y respuesta de bid duplicado.

### Conclusión

**AD3 conserva su valor como disciplina de recuperación e idempotencia, pero el flujo de negociación que motivó sus plazos de 30 segundos fue retirado en E2.** Los mecanismos E1 de reporte siguen vigentes; adaptar el seguimiento de bids y swaps a la wallet está pendiente y no se acredita en producción.
