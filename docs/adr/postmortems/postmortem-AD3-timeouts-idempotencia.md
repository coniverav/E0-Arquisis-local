# Post-mortem AD3: Timeouts e idempotencia de operaciones (E2-082)

**Revisión retrospectiva:** 3 de noviembre de 2026. El análisis contrasta [AD3](../ADR-003-timeouts-idempotency.md) con el estado actual del repositorio, diferenciando el flujo heredado de E1 de los nuevos mecanismos v3.

### Decisión y resultado de E1

En E1 se eligieron plazos y reintentos durables en PostgreSQL con un `cycle-scheduler` independiente. Esto evitó mantener temporizadores de negocio exclusivamente en memoria de una réplica de `master`. El `idpk` identificaba la operación y el `msgId` una publicación concreta; los reintentos de `negotiation-proposal` y algunos de `negotiation-report` podían conservar la primera identidad y emitir un mensaje nuevo. [`idempotency.py`](../../../master/app/services/idempotency.py) reclama el `idpk` en la misma transacción que su aplicación, y el outbox separa la intención de publicación del envío RabbitMQ.

El tradeoff de E1 fue aumentar estados, consultas y trabajo del scheduler para evitar repetir efectos ante reentregas y reinicios. El reporte por ciclo y sus reintentos siguen teniendo tratamiento propio.

### Cambio real al protocolo v3

La negociación `negotiation-proposal → give/take → transferencia de pago` de E1 fue retirada del mercado de E2. **`negotiation-report` sigue vigente**, por lo que no deben mezclarse los plazos del reporte con los de propuestas, bids o swaps.

En el código E2 se reaprovecha **la disciplina** de durabilidad, no los deadlines de 30 segundos del negocio retirado:

- [`seller_bid_acceptance.py`](../../../master/app/services/seller_bid_acceptance.py) registra reserva e intención de swap en PostgreSQL, coordinadas por la fila de oferta (AD4).
- [`seller_swap_dispatch.py`](../../../master/app/services/seller_swap_dispatch.py) utiliza `FOR UPDATE SKIP LOCKED`, lease, `proposal_expires_at` persistido y reintentos con la misma `Idempotency-Key`; el HTTP sucede tras cerrar la transacción.
- [`swap_accept.py`](../../../master/app/services/swap_accept.py), [`wallet_recovery.py`](../../../master/app/services/wallet_recovery.py) y [`swap_timeouts.py`](../../../master/app/services/swap_timeouts.py) distinguen respuestas HTTP inciertas, estados confirmados de la wallet y liberación de reservas con evidencia. Una respuesta `202` no cierra el ciclo económico.
- [`operational_rate_limits.py`](../../../master/app/services/operational_rate_limits.py) persiste cupos de lectura/escritura y bloqueos por `Retry-After` compartidos por ciudad, para no multiplicar el presupuesto de llamadas por réplica.

### Evidencia disponible

- [`test_e2068_wallet_recovery.py`](../../../master/tests/test_e2068_wallet_recovery.py) contempla `202` perdido, GET con estado `accepted`/`settled`, 404/timeout indeterminado, eventos durante el HTTP y estados terminales que no retroceden.
- [`test_e2071_operational_limits.py`](../../../master/tests/test_e2071_operational_limits.py) incluye cupos compartidos entre sesiones, bloqueo `Retry-After`, cuota de publicación y simultaneidad.
- [`test_negotiation_timeouts.py`](../../../master/tests/test_negotiation_timeouts.py) y [`test_negotiation_retries.py`](../../../master/tests/test_negotiation_retries.py) conservan el alcance histórico de E1.

Los archivos prueban cobertura implementada, pero no constituyen por sí solos evidencia de ejecución en producción, caída real de wallet o reinicio de worker con un swap pendiente.

### Tradeoff, divergencia y riesgo residual

**Se conserva AD3 como regla de reintento e idempotencia, con una máquina de estados diferente para E2.** El estado durable facilita recuperar incertidumbres, pero exige leases, expiraciones, ventanas oficiales, reintentos y conciliación; puede haber más de una solicitud HTTP con la misma llave sin que deba haber dos operaciones económicas. La wallet continúa siendo la autoridad sobre el resultado del swap.

**Divergencia observable en código:** [`master/app/workers/cycle_scheduler.py`](../../../master/app/workers/cycle_scheduler.py) todavía invoca `process_expired_negotiations` y `enqueue_timed_out_negotiation_retries`, servicios heredados de E1. Su presencia **no demuestra** que se estén generando negociaciones v1 en producción, pero tampoco permite afirmar que el flujo retirado esté completamente desactivado. Debe verificarse si existen registros `Negotiation` elegibles y si alguna ruta aún los crea; el mecanismo de `negotiation-report` no debe eliminarse en esa revisión.

Otra limitación: sin eventos oficiales o una consulta concluyente a wallet, un POST ambiguo o un GET 404 no justifica liberar `HOLD` ni generar otro swap. Los procesos deben conservar esa incertidumbre para recuperación o conciliación posterior.

### Cierre y acciones pendientes

AD3 queda **reutilizada para swaps, bids y cuotas**, sin trasladar los plazos de negocio E1 al protocolo v3. Queda pendiente revisar el uso efectivo de los servicios de negociación heredados y ejecutar una prueba controlada de reinicio/corte con respuesta perdida que demuestre recuperación sin segunda operación económica. Los reintentos del reporte se evalúan separadamente.
