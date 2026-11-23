# E3-047 — Callback comprador tras hecho económico persistido

El único escritor de efectos económicos sigue siendo `WalletService.process_swap_event` del Master E2. Ni Lambda ni el relay escriben el ledger, reservas o `remaining`.

1. `swap-settled`, `swap-rejected`, `swap-expired` y `swap-cancelled` ingresan por EC2/Connector, se deduplican y se aplican al `SwapRecord`/ledger mediante Master, con commit antes del callback.
2. `POST /internal/saga-callbacks/claims` selecciona exclusivamente un task token `TERMINAL` correlacionado simultáneamente por `sagaExecutionId`, `swapId`, rol comprador y estado Wallet **persistido**. El predicado sirve también si el evento llegó antes del registro del token. Usa `FOR UPDATE SKIP LOCKED` y lease recuperable.
3. El relay independiente de Connector ejecuta `SendTaskSuccess` con el task token original y un resultado mínimo (`outcome`, `swapId`). Nunca publica esos tokens ni JWT en logs.
4. Sólo después de un ACK AWS, `POST /internal/saga-callbacks/{id}/result` cambia a `SUCCEEDED` con fencing del claim. Un error transitorio vuelve a `WAITING` con backoff. Un `TaskDoesNotExist`, `TaskTimedOut` o `InvalidToken` da `AMBIGUOUS`: no se finge éxito y requiere reconciliación operativa.
5. El cierre `finalize-saga BUYER` sólo acepta `SETTLED` tras observar `SwapRecord.SETTLED`, o `REJECTED` tras un hecho Wallet terminal. `INVALID_SWAP_TOKEN` / `DEADLINE_EXPIRED` pueden generar rechazo local sólo si nunca se intentó HTTP. `TIMED_OUT` no implica estado económico final.

`E3_BUYER_CALLBACK_RELAY_ENABLED=false` por defecto, para no activar el flujo AWS antes de las pruebas end-to-end. El endpoint de claims pertenece a la misma **red interna** que el outbox relay y no debe exponerse al público; controlar acceso a esa red antes de cualquier despliegue.

## Límites y continuidad

- La entrega física de un callback sigue siendo al menos una vez. Una caída posterior a `SendTaskSuccess` y anterior al ACK local puede producir una respuesta `TaskDoesNotExist`, registrada como `AMBIGUOUS`, nunca como éxito inventado.
- Un `swap-settled` tardío después de un rechazo o timeout puede modificar el ledger E2. No revierte por sí solo una ejecución AWS ya cerrada; se registra como desviación para conciliación en E3-048.
- La Lambda `reconcile-buyer-swap` por GET Wallet sigue siendo de solo lectura; no se concluye `SETTLED` sin un efecto de ledger local. Las pruebas integrales AWS y el tratamiento del estado incierto siguen pendientes.
- Ningún registro `SAGA_START` ni flujo E2 anterior modifica su comportamiento con el flag del relay desactivado.
