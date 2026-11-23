# E3-048 — Recuperación por timeout y conciliación posterior a cancelación

## Fronteras

- `reconcile-seller-swap` / `reconcile-buyer-swap` verifican identidad de saga y `swapId` contra PostgreSQL **antes** de ejecutar `GET /swaps/{swapId}`.
- La respuesta GET debe ser HTTP 200, tener exactamente el mismo `swapId` y un estado Wallet reconocido. El input y output de Step Functions llevan solo `status`, `swapId` y, si procede, `expiresAt`, sin JWT ni otros datos de Wallet.
- `cancel-seller-swap` verifica la llave estable calculada por `seller_saga_intent`, consulta Wallet, envía POST `/cancel` solo si el swap sigue vivo, y vuelve a realizar GET. Si el POST retorna 202, falla o queda ambiguo, **no se declara liberado el HOLD**. Si ya está terminal, no realiza otro POST.
- E2 `swap_timeouts` continúa siendo el camino de conciliación de hechos Wallet al ledger: después de pedir cancelación, realiza GET y usa el mismo `WalletService.process_swap_event` transaccional para aplicar la confirmación económica. Un timeout/error/404/estado activo NO libera reserva.
- No se modifica el ledger en Lambda ni en el relay de callbacks. Las funciones de finalización E3-042 y E3-047 exigen estado económico ya persistido.

## Indicador AD7

`GET /internal/saga-recovery/health` (acceso interno) informa `overdueExecutionsAfter60s`, `heldReservationsAfterClose` y `unresolvedTimedOutOrFailedSwaps`, con muestras limitadas de `idpk`. No altera datos. Objetivo operativo: cero ejecuciones activas 60 segundos después de `marketClosesAt` y cero reservas `HOLD` posteriores al cierre. Si hay casos, se conservan para investigación en lugar de fingir éxito.

## Despliegue y límites

La validación local no equivale a una prueba end-to-end. Los flags de sagas y relays permanecen desactivados en producción hasta completar E3-049 y E3-055–062. Comprobar que los controles internos de red/autenticación restringen el nuevo endpoint antes de desplegar; no publicar su respuesta al frontend público. No reintentar cancelaciones con llaves nuevas y no cerrar una saga como `SETTLED` sin hecho Wallet persistido.
