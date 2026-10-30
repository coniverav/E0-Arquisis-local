# E2-068 — Recuperación de 202 perdido y reordenamiento Wallet (AD5)

## Contrato de recuperación

Se conserva siempre la misma operación económica en `SwapRecord` y la misma `Idempotency-Key` para una repetición de `POST /swaps` o `POST /swaps/{id}/accept`.

1. **Antes del primer POST** se persiste la intención y su llave. `expiresAt` de propuesta permanece congelado.
2. **Respuesta 202** significa recepción, **no** liquidación; no genera asiento `SWAP_SETTLED` ni libera `HOLD`.
3. **Timeout o red caída después del POST**: resultado indeterminado. Nunca crear otro swap ni otra llave. La recuperación por vendedor reusa el registro E2-032; la del aceptante reusa E2-041.
4. **Si se conoce `swapId`**, antes de reintentar se realiza `GET /swaps/{id}`. Se valida que la identidad coincida; `settled`, `expired`, `cancelled` y `rejected` siguen el mismo servicio idempotente de eventos Wallet E2-021/051. Para un comprador, `proposed` no prueba que el `accept` se ejecutó: puede repetir *la misma* aceptación. `accepted` de Wallet tampoco implica `settled`.
5. **Si no se conoce `swapId`**, no existe GET oficial por `Idempotency-Key`: se espera `swap-proposed` o se repite el POST *sin cambiar* `Idempotency-Key` y `expiresAt`, dentro del plazo. Después del vencimiento, una propuesta que ya tuvo intento queda **indeterminada**, sin liberar reservas por conjetura.
6. **Eventos reordenados**: `swap-settled` puede llegar antes de recibir 202 (o durante un GET). Se refresca el registro con bloqueo al finalizar el HTTP; un estado terminal no se degrada a `SUBMITTED` o `ACCEPT_SUBMITTED`.
7. **Replays**: la aplicación económica y reserva son idempotentes por `swapId`, no por número de intentos HTTP. Si el mensaje AMQP fue duplicado se mantiene un único efecto económico. La Wallet sigue siendo fuente de verdad.
8. **GET 404 o fallido** NO equivale a 'el POST nunca ocurrió', especialmente ante eventuales retrasos de visibilidad. No generar otra operación ni liberar un HOLD incierto. Si la situación persiste, revisar eventos pendientes y la conciliación `/statement` E2-044.

## Casos controlados para revisión

| Caso | Resultado esperado |
|---|---|
| POST proposer procesado pero respuesta 202 perdida, no se conoce swapId | Reenvío con la *misma* llave antes de vencimiento; `swap-proposed` correlaciona y detiene reintentos |
| POST accept procesado, timeout y `GET` devuelve `accepted` | No repetir POST; se espera `swap-settled` |
| POST accept procesado, timeout y `GET` devuelve `settled` | Consumir hecho confirmado, sin nueva operación |
| GET devuelve `proposed` para aceptante | No demuestra aceptación: se puede reintentar con la llave existente |
| GET devuelve 404/timeouts | Mantener incertidumbre; no liberar reservas ni inventar swap nuevo |
| `swap-settled` llega durante el POST | El HTTP tardío no sobrescribe `SETTLED` ni duplica ledger |
| Propuesta vencida después de algún intento y sin swapId | Mantener como indeterminada (reconciliación manual/por eventos), sin afirmar `NEVER_SENT` |

## Limitaciones y decisiones

- Esta implementación NO introduce un endpoint ficticio GET por idempotency-key.
- Los reintentos HTTP *con la misma llave* pueden producir dos solicitudes HTTP, pero no dos operaciones económicas cuando Wallet cumple el contrato de idempotencia. Dos workers concurrentes pueden coexistir; la propuesta de vendedor mantiene lease PostgreSQL E2-032.
- Una propuesta sin `swapId` cuyo POST tuvo resultado incierto al expirar requiere conciliación con eventos Wallet o statement. No se resuelve arbitrariamente.
- `GET /swaps/{id}` puede informar un estado sin saldos. La conciliación E2-044 es la autoridad para diferencias pendientes de `balanceAfter`.
- E2-068 no modifica las migraciones ni el contrato económico.