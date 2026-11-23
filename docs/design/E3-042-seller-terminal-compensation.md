# E3-042 — Finalización y compensación seller

## Alcance

La saga seller cierra `SETTLED`, `REJECTED` (rechazo anterior a la propuesta Wallet) y `COMPENSATED`. El estado económico continúa siendo responsabilidad del ingreso autenticado de eventos Wallet en `master/app/services/wallet_service.py`.

* `swap-settled`: el servicio existente aplica ledger, convierte `HOLD` en `COMMITTED`, descuenta `remaining` y encola `offer-updated` cuando corresponde, en su misma transacción.
* `swap-rejected`, `swap-expired`, `swap-cancelled`: ese servicio convierte `HOLD` en `RELEASED`. No descuenta `remaining`.
* Lambda `compensate-seller` no libera nada por sí sola: exige `SwapRecord` terminal **y** reserva `RELEASED`; cierra `SagaExecution` como `COMPENSATED` en una única transacción.
* Lambda compartida `finalize-saga`, con rol `SELLER`, exige `SwapRecord.SETTLED` y reserva `COMMITTED` antes de escribir `SagaExecution.SETTLED`. Para `REJECTED`, exige rechazo anterior a cualquier swap/reserva.
* Conclusión idempotente: el segundo llamado devuelve `alreadyFinalized`; no vuelve a modificar la oferta, el ledger ni las reservas. Los task tokens aún `WAITING` se marcan `SUPERSEDED` al terminar localmente.
* Ante inconsistencia entre evento, swap y reserva, no se infiere estado económico ni se libera energía. `SUBMITTED`/`PROPOSED` generan `SellerTerminalPending`.

## Integración Step Functions / Wallet

Se agrega manejo de las dos tareas faltantes y reintentos acotados para eventos recién ingresados. La respuesta del adaptador Wallet normaliza `status` a mayúsculas en el nivel de `Payload`, manteniendo `wallet` sin cambios, y expone `swapId`/`expiresAt` sólo desde HTTP 200. Una respuesta HTTP 202 no confirma ningún estado.

`ReconcileProposal` puede recuperar el `swapId` guardado en PostgreSQL a partir de `sagaExecutionId` e `idempotencyKey`; si el valor aún no es conocido **no** hace una consulta Wallet ficticia y termina en revisión/conciliación posterior. El `swapId` recibido se compara con el persistido.

## Limitaciones actuales

E3-042 no instala el worker que correlaciona eventos Wallet y llama `SendTaskSuccess` sobre el token del callback; esta integración y la conciliación de callbacks tardíos quedan para E3-047. Tampoco habilita los feature flags en producción. Las pruebas de esta tarjeta comprueban transacciones y estados PostgreSQL, no Step Functions/AWS reales.

No se reemplaza el flujo E2 de liquidación ni se escriben mensajes RabbitMQ desde las Lambdas. El retry no crea hechos de Wallet y un estado incierto puede dejar la saga en error hasta su conciliación, en lugar de liberar reservas indebidamente.
