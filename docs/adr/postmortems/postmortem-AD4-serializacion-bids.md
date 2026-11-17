# Post-mortem AD4: Serialización de la aceptación de bids

**Revisión retrospectiva:** 17 de noviembre de 2026. Se contrasta [AD4](../ADR-004-serializacion-aceptacion-bids.md) con la evidencia local y operativa reunida durante E2, sin convertir una prueba controlada en una afirmación sobre Wallet Central.

### Decisión y resultado de E2

AD4 eligió bloqueo transaccional por oferta y reservas persistidas para evitar que bids concurrentes comprometieran más que el `remaining`. El escenario comprometido era una oferta de 100 KWH con dos bids de 80 KWH iniciados con cerca de 40 ms de separación: debía quedar una reserva de 80 KWH, un rechazo `QUANTITY_UNAVAILABLE` con `available=20` y ninguna segunda intención de swap.

[`seller_bid_acceptance.py`](../../../master/app/services/seller_bid_acceptance.py) bloquea la oferta con `SELECT FOR UPDATE`, considera las reservas `HOLD` al calcular disponibilidad y persiste `OfferReservation` y `SwapRecord` antes de llamar a Wallet. [`seller_swap_dispatch.py`](../../../master/app/services/seller_swap_dispatch.py) reclama el despacho con `SKIP LOCKED`, mantiene la misma `Idempotency-Key` y ejecuta el HTTP después de cerrar el bloqueo. Un `202` no genera por sí solo `bid-accepted`: el hito posterior es `swap-proposed`.

### Evidencia disponible

- [`test_bids_concurrentes.py`](../../../master/tests/test_bids_concurrentes.py) usa sesiones PostgreSQL concurrentes para cubrir la oferta 80+80, ráfagas, duplicados por `idpk`, rollback, locks superpuestos y ofertas independientes.
- [`datos_e2069/RESUMEN_E2_069.md`](../../../datos_e2069/RESUMEN_E2_069.md) registra 13 escenarios locales `PASS` del 30 de octubre. En 80+80 quedó un `HOLD` de 80 KWH, un rechazo con 20 KWH disponibles y una sola intención de swap. Los offsets fueron 1,68 ms y 41,48 ms; la separación de 40 ms es nominal entre inicios, no una medida de latencia ni de orden de locks.
- [`test_seller_bid_accepted.py`](../../../master/tests/test_seller_bid_accepted.py), [`test_incoming_bids.py`](../../../master/tests/test_incoming_bids.py) y [`test_wallet_events_v3.py`](../../../master/tests/test_wallet_events_v3.py) cubren rechazo, reserva, transición posterior a `swap-proposed` y conservación del motivo de un `swap-rejected`.
- **Observación operativa de E2:** al revisar swaps se encontraron 341 eventos `swap-rejected` que traían `reason=INSUFFICIENT_ASSET`, mientras `SwapRecord.rejection_reason` contenía `UNKNOWN_REJECTION`. El contrato trae `reason` en el nivel superior del evento y el procesamiento no lo propagaba. El mapeo fue corregido y probado; es evidencia de un problema de trazabilidad, no de doble reserva.

Las pruebas y el registro acreditan el comportamiento diseñado en PostgreSQL aislado. No hay una corrida adjunta con dos compradores reales, ambas réplicas desplegadas y Wallet Central. Tampoco existe una serie de producción que correlacione cada `INSUFFICIENT_ASSET` con oferta, `HOLD` y balance oficial.

### Tradeoff y divergencias

**La decisión de serializar por oferta se mantiene.** El lock corto y la reserva durable coordinan instancias sin retener la fila durante la latencia HTTP. A cambio se agregan estados `PENDING`, `RESERVED`, `HOLD`, `SUBMITTED` y terminales, junto con recuperación para respuestas inciertas de Wallet.

La divergencia principal entre el escenario probado y producción es la Wallet. Un `INSUFFICIENT_ASSET` puede responder a saldo o activo insuficiente y no demuestra por sí solo que KLD haya aceptado dos veces la misma energía. La corrección de `reason` permite investigarlo sin ocultar la causa, pero no reemplaza esa correlación faltante.

### Cierre y acciones pendientes

AD4 queda **conservada para E3**. La saga vendedora debe comenzar sólo después de la reserva transaccional, usar el `idpk` del bid como identidad de ejecución y conservar la relación entre reserva, swap, task token y ejecución. Sólo puede liberar un `HOLD` con evidencia de rechazo, expiración, cancelación o conciliación concluyente; un timeout o HTTP perdido no basta.

Falta medir por ciclo reservas `HOLD` y `remaining`, ejecuciones repetidas por el mismo `idpk` y rechazos de Wallet correlacionados por bid, oferta, activo y balance. Esa evidencia permitirá separar un defecto de concurrencia local de una insuficiencia legítima de Wallet.
