# E3-027 — Inicio de saga seller en la misma transacción que AD4

## Alcance y dependencia

**Depende de** E3-025/026 (tabla `outbound_messages` ampliada; `enqueue_saga_start`, `claim_batch`, `finish_claim`) y E3-017/018 (máquinas Step Functions Standard seller/buyer y su contrato de entrada). Este cambio conecta **el lado vendedor** a la intención durable de inicio y no ejecuta todavía AWS `StartExecution`.

El ADR-008 en `docs/adr/ADR-008-outbox-confiable.md` sigue siendo la decisión fuente: no se agrega tabla paralela, se registra la intención en la misma transacción y el relay realizará como mínimo una entrega, con identificadores reutilizables.

## Camino seller

1. `process_incoming_bid` persiste `MarketBid(PENDING)` y valida el protocolo.
2. `reserve_incoming_bid` ejecuta AD4: bloquea primero `MarketOffer` y luego `MarketBid` (`FOR UPDATE`), comprueba disponibilidad y guarda `OfferReservation(HOLD)`, `SwapRecord(SUBMITTED)`, `MarketBid(RESERVED)`.
3. **Sólo si `E3_SELLER_SAGA_ENABLED=true`**, en esa misma `Session`, `enqueue_seller_saga_on_reserve()` construye un payload fijo, lo valida y llama `enqueue_saga_start()` sin commit. Se almacena `delivery_kind=STEP_FUNCTIONS`, `message_type=SAGA_START`, `execution_name=bid.idpk` y `state_machine_arn` del flag.
4. El llamador (`/internal/messages` o `cycle-scheduler` con FIFO/BATCH/MAX_REMAINING) hace el **único commit**. Si falla el INSERT en outbox, toda la reserva económica se revierte.
5. Tras el commit, un relay futuro reclama el outbox y llama `StartExecution`; si ocurre un crash, el mismo `msgId`/`idpk`/input queda disponible para reintento.

## Identidad y entradas

- `execution_name` **igual al `idpk` del `bid`**; la `Idempotency-Key` de propuesta Wallet es ese mismo `idpk`, ya persistido en E2.
- `execution_input` es JSON canónico inmutable. Incluye `schemaVersion`, `role=SELLER`, `idpk`, `cycleId`, `marketClosesAt`, el bid (`msgId`, `offerId`, `buyerCityId`, `quantityKwh`) y el SwapRecord (`give/take` **como cadenas decimales**, `memo`, `idempotencyKey`). Nunca lleva `WALLET_API_KEY` ni task token.
- `expires_at` del outbox **nulo deliberadamente**: aunque el inicio se recupere después de `marketClosesAt`, la saga debe iniciar para compensar la reserva. No puede proponer un swap ya vencido; la máquina E3-017/018 debe interpretar `marketClosesAt` y compensar/terminar.
- El `msgId` del outbox de Step Functions es sólo la identidad local de esa intención. El `idpk` original es el nombre de ejecución y no se regenera.

## Evitar doble coordinación E2/E3

El worker E2 `seller_swap_dispatch._claim_one` **excluye permanentemente los swaps que tengan `SAGA_START`**. El flag desactivado conserva exactamente el camino E2. Esta exclusión debe mantenerse incluso si la fila outbox está `FAILED`, pues el relay E3 es quien debe recuperarla.

**No activar el feature flag en producción en esta tarjeta.** Hasta que existan máquinas y relays, encenderlo dejaría HOLD sin propuesta de Wallet. Coordinar el corte con E3-017/018 y posteriores tarjetas de relay/consumidor y compensación de saga. No ejecutar dos coordinadores por swap.

## Pruebas/evidencia

`./probar_E3_027.sh` aplica el parche, valida sintaxis, crea solamente `energyshark_test_e3027` (host Docker `db`), ejecuta PostgreSQL real + FastAPI + regresión E2. Cubre:

- Atomicidad ante `rollback` e interrupción de INSERT en outbox.
- Crash simulado **tras commit**, sesión nueva y recuperación de claim/lease; `claim_token` obsoleto no confirma.
- Duplicados de `bid`/ejecución, dos bids simultáneos sin sobreventa AD4, varios swaps distintos, rechazo de cantidades mayores.
- Ambos caminos de reserva: ingreso HTTP FIFO y `process_due_seller_bids` del scheduler.
- Exclusión del worker Wallet E2 para operaciones propiedad de Step Functions; modo E2 sigue operativo con flag apagado.
- No hay llamadas reales a AWS ni RabbitMQ durante estas pruebas. El nombre de ejecución duplicado lo garantiza la restricción PostgreSQL de E3-026 y la semántica Standard de AWS; el relay real y su integración se validarán en tarjetas posteriores.

## Criterios para PR

- Tests verdes, migraciones E3-026 verificadas y diffs revisados.
- `E3_SELLER_SAGA_ENABLED` ausente/false tanto en producción como en el scheduler hasta completar el cutover.
- Revisión de al menos dos integrantes si se utiliza programación agéntica, de acuerdo con la política E3 del proyecto.
- ADR-008 anterior a implementación (ya existe en develop).
