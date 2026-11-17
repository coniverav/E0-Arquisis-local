# Post-mortem AD4: Serialización de la aceptación de bids

**Revisión retrospectiva:** 17 de noviembre de 2026. Este documento contrasta el escenario prometido por [AD4](../ADR-004-serializacion-aceptacion-bids.md) con la evidencia local y operativa reunida durante E2. Distingue una prueba de concurrencia reproducible de lo que fue observado en el mercado real.

## Escenario prometido

AD4 buscaba evitar que dos bids concurrentes comprometieran más energía que el `remaining` de una misma oferta. El escenario concreto era recibir dos bids de 80 KWH con aproximadamente 40 ms de separación sobre una oferta de 100 KWH. El resultado esperado era una única reserva de 80 KWH y un rechazo `QUANTITY_UNAVAILABLE` con `available=20`, sin un segundo swap ni un rechazo de Wallet atribuible a doble aceptación propia.

La decisión fue usar un bloqueo transaccional por oferta y reservas persistidas. El HTTP hacia Wallet ocurriría después de cerrar la transacción; un `202` no sería una aceptación comercial ni una liquidación.

## Qué ocurrió en E2

El mecanismo quedó implementado sobre PostgreSQL. Al reservar un bid, el backend bloquea la oferta con `SELECT FOR UPDATE`, descuenta las reservas `HOLD` de la disponibilidad, y persiste `OfferReservation` y `SwapRecord` antes de que el despachador consulte Wallet. El despachador reclama trabajos con `SKIP LOCKED`, conserva la `Idempotency-Key` y no publica `bid-accepted` sólo por recibir HTTP `202`.

La evidencia reproducible más directa es la ejecución local registrada el 30 de octubre en [`datos_e2069/RESUMEN_E2_069.md`](../../../datos_e2069/RESUMEN_E2_069.md). Los 13 escenarios registrados como `PASS` incluyeron dos bids 80+80, ráfagas de ocho compradores, reentrega de un mismo `idpk`, rollback y un lock sostenido 120 ms con el segundo envío iniciado 40 ms después. En el caso 80+80 se observó una reserva `HOLD` de 80 KWH, un rechazo con 20 KWH disponibles y una sola intención de swap. Los offsets registrados fueron 1,68 ms y 41,48 ms; por lo tanto, 40 ms describe la separación observada entre inicios, no un límite de latencia ni de adquisición del lock.

También hubo señales operativas que deben interpretarse con cuidado. En la investigación posterior de swaps se identificaron **341 eventos `swap-rejected`** cuyo mensaje indicaba `INSUFFICIENT_ASSET`, mientras el campo local `SwapRecord.rejection_reason` se había guardado como `UNKNOWN_REJECTION`. La causa fue una pérdida de trazabilidad: el contrato entrega `reason` en el nivel superior del evento y el procesamiento no lo estaba propagando. Se corrigió el mapeo y se cubrió con pruebas de `swap-rejected` y de recuperación desde Wallet. Ese problema ocultaba el motivo real del rechazo, pero por sí solo **no demuestra** una doble reserva ni permite atribuir los 341 casos a AD4: `INSUFFICIENT_ASSET` puede depender del activo, la Wallet o una operación ajena a dos bids locales concurrentes.

## Qué evidencia respalda la decisión

- [`test_bids_concurrentes.py`](../../../master/tests/test_bids_concurrentes.py) ejercita sesiones PostgreSQL concurrentes y verifica reservas, `remaining`, respuestas de rechazo, rollback e idempotencia por `idpk`.
- [`test_incoming_bids.py`](../../../master/tests/test_incoming_bids.py) cubre la validación y el rechazo de bids por disponibilidad.
- [`test_seller_bid_accepted.py`](../../../master/tests/test_seller_bid_accepted.py) comprueba que `swap-proposed` es el hito previo a `bid-accepted` y cubre un rechazo `INSUFFICIENT_ASSET` con liberación de reserva.
- [`test_wallet_events_v3.py`](../../../master/tests/test_wallet_events_v3.py) conserva el `reason` que llega en un evento oficial `swap-rejected`.

Estas pruebas y el registro local muestran que el mecanismo de reserva se comportó como se diseñó en un PostgreSQL aislado. No equivalen a una ejecución con dos compradores reales, las dos réplicas desplegadas y Wallet Central. Tampoco contamos con una serie de métricas de producción que conecte cada rechazo `INSUFFICIENT_ASSET` con la oferta, los `HOLD` y el saldo oficial de la Wallet.

## Balance y cambios propuestos para E3

La decisión de serializar por oferta se mantiene: el lock corto y la reserva durable siguen siendo la protección local adecuada para no sobrecomprometer energía entre réplicas. El costo observado es una mayor cantidad de estados (`PENDING`, `RESERVED`, `HOLD`, `SUBMITTED` y terminales), además de recuperación cuando una respuesta de Wallet es incierta.

E3 no debe reemplazar esta garantía al introducir Step Functions. La saga vendedora debe iniciar sólo después de la reserva transaccional, usar el `idpk` del bid como identidad de ejecución, y conservar la asociación entre reserva, swap, task token y ejecución. Sus compensaciones deben liberar un `HOLD` únicamente después de evidencia de rechazo, expiración, cancelación o una conciliación concluyente; un timeout o HTTP perdido no basta.

Para cerrar la validación pendiente, se propone medir durante E3: (1) reservas `HOLD` y `remaining` por oferta al cierre, (2) ejecuciones de saga repetidas por un mismo `idpk`, y (3) rechazos de Wallet clasificados por activo y motivo, correlacionados con bid, oferta y balance oficial. Así se podrá distinguir un defecto de concurrencia local de una insuficiencia legítima de la Wallet.
