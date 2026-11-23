# E3-038–E3-048 — Integración seller/buyer: estado y orden de trabajo

Este documento es un análisis del código existente en `develop` al iniciar el bloque.
**No es evidencia de ejecución productiva ni implica que las 11 tarjetas estén completas.**
El feature flag `E3_SELLER_SAGA_ENABLED` permanece apagado por defecto.

## Integración inicial de E3-038

`reserve_incoming_bid` (AD4) ya bloquea la oferta, valida `minBid`, genera una
reserva `HOLD`, un `SwapRecord(SUBMITTED)` y la intención `SAGA_START` dentro
de la misma transacción (E3-027). Este parche añade un `SagaExecution` local
con el mismo `idpk`, para permitir registrar `taskToken` y correlacionar los
callbacks antes de una confirmación de AWS. La fila y el outbox desaparecen
juntos si el llamador hace rollback.

El contrato de entrada ahora publica los campos requeridos al inicio por la
ASL seller (`offerId`, `quantity`, `sagaExecutionId`, `timeoutAt`, `timeouts`)
sin perder el bid y la intención económica original. La identidad local del
callback es el `idpk` original; no se genera una segunda clave. La llave de
cancelación es determinística y diferente a la de la propuesta Wallet.

**Esto completa una parte de la integración durable, no el flujo seller.**
El relay de E3-029–031 sigue a cargo de `StartExecution` externo. Las pruebas
locales no son evidencia de una ejecución Step Functions.

## Componentes para completar por tarjeta

| Tarjeta | Reutilizable | Integración todavía necesaria |
| --- | --- | --- |
| E3-038 | AD4 `reserve_incoming_bid`, outbox y relay Step Functions | Evidencia StartExecution real con flag, ARN y callbacks end-to-end |
| E3-039 | Rechazo transaccional y reserva existente | Lambda `reserve-seller` sólo debe verificar un HOLD, nunca reservar nuevamente; `reject-seller-bid` no debe emitir doble rechazo |
| E3-040 | Lambda wallet `seller-proposal`, reintentos e idpk | Congelar `expiresAt` en SwapRecord, enviar cuerpo cerrado de Wallet y conectar `swap-proposed` a task token; 202 no termina nada |
| E3-041 | `enqueue_bid_accepted_after_swap_proposed` en ingreso Wallet | Tarea `publish-bid-accepted` debe verificar que existe evento Wallet persistido y usar la fila outbox, sin duplicados |
| E3-042 | `OfferReservation` y manejo E2 de eventos Wallet | Lambda `compensate-seller` sólo libera HOLD después de estado terminal corroborado; finalización durable |
| E3-043 | `seller_offer_events.enqueue_confirmed_offer_update` | Verificar que settlement y offer-updated comparten transacción; no decrementar remaining dos veces |
| E3-044 | `process_bid_reply`, outbox StartExecution | Crear intención buyer una sola vez y excluir su operación de `accept_pending_swaps` E2 |
| E3-045 | `swap_tokens.verify_swap_token` usa JWKS | Adaptador Lambda `validate-buyer-token` ES256 y `check-buyer-deadline`, sin filtrar el token al execution input |
| E3-046 | Lambda wallet `buyer-acceptance` y callbacks | Persistir ejecución/SwapRecord e idempotencia de aceptación; `202` sigue esperando Wallet |
| E3-047 | `wallet_recovery` y ledger existente | Reconciliar en callback por `swapId`, resolver orden/duplicados y sólo aplicar un efecto económico |
| E3-048 | `swap_timeouts` y Lambda `cancel-seller-swap` | Conciliar GET posterior a cancelación/timeout y medir cero HOLD/ejecuciones abiertas +60 s |

## Bloqueos reales que impiden habilitar E3

1. `infra/cdk/lambda_src/saga_task/handler.py` todavía lanza
   `SagaTaskNotImplemented` para varias tareas definidas en las ASL
   (`reserve-seller`, `reject-seller-bid`, `publish-bid-accepted`,
   `compensate-seller`, `validate-buyer-token`, `check-buyer-deadline` y
   `finalize-saga`). No activar E3 hasta implementarlas y probarlas.
2. La ASL seller intenta reconciliar la propuesta por `idempotencyKey`, pero
   el adaptador GET requiere `swapId`. Debe congelarse/persistirse el `swapId`
   recuperado de HTTP/evento o mantenerse el caso indeterminado, sin liberar.
3. La ASL usa `Payload.status` para reconciliación, mientras el adaptador
   Wallet devuelve `Payload.wallet.status`. Normalizar explícitamente.
4. La propuesta Wallet tiene un contrato cerrado: sólo `give`, `take`,
   `expiresAt`, `memo`; no reenviar el input interno completo de saga al POST.
5. El campo `timeoutAt` de este parche es un límite superior de la saga,
   no necesariamente el vencimiento del callback Step Functions. Deben
   definirse y probarse deadlines por tarea y timeout/reconciliación segura.
6. El corte E2→E3 necesita coordinación con el equipo y pruebas de no doble
   coordinación para seller **y** buyer antes de habilitar flags/relays.

## Validación local del primer parche

Con WSL/Docker funcionando y `DATABASE_URL` apuntando a una PostgreSQL
**descartable** que comience por `energyshark_test_`, ejecutar desde la raíz:

```bash
python -m unittest discover -s infra/cdk/tests -p 'test_e3038*.py' -v
cd master
python -m alembic heads  # debe existir un único head del equipo
python -m pytest -v tests/test_e3038_seller_saga_start.py tests/test_e3027_seller_saga_db.py
```

No publicar una demo real ni habilitar flags tras aprobar sólo estas pruebas.
Las 11 tarjetas requieren PRs/evidencias adicionales por fases.
