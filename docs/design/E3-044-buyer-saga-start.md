# E3-044 — Inicio idempotente de saga comprador

## Fuente y fronteras

- `process_bid_reply` valida el `bid-accepted` contra `MarketBid`/outbox original.
- Si `E3_BUYER_SAGA_ENABLED=true`, dentro de la misma transacción del inbox crea
  `SwapRecord(ACCEPTOR, SUBMITTED)`, `SagaExecution(BUYER, PENDING_START)` y
  `OutboundMessage(SAGA_START, STEP_FUNCTIONS)` con el `idpk` de `bid-accepted`.
- El JWT `swapToken` se conserva **únicamente** en `SwapRecord`, no en
  `execution_input`, ni en los logs del relay o Step Functions. E3-045 verificará
  firma ES256/JWKS y claims antes de cualquier operación Wallet.
- El `accept_idempotency_key` es el idpk recibido de `bid-accepted`: no varía
  por entregas duplicadas, redelivery ni relanzamiento del worker.
- No se invoca AWS ni Wallet desde el ingreso. El relay actual publicará la
  intención sólo después del COMMIT, con `execution_name=bid-accepted.idpk`.
- El worker E2 excluye registros con una ejecución BUYER existente, incluso si
  luego se apaga el flag. Otras operaciones E2 preexistentes siguen en su worker.

## Activación

Por defecto, `E3_BUYER_SAGA_ENABLED=false`; con el flag activado se requiere
`E3_BUYER_STATE_MACHINE_ARN` válido. Si falta, se revierte toda la transacción
correspondiente a la aceptación, incluida la modificación de `MarketBid`.

No activar el flag en producción hasta completar E3-045/046/047/048 y validar
la convivencia/corte E2→E3 con las dos réplicas y el relay real.

## Pruebas

```bash
python -m unittest discover -s infra/cdk/tests -p 'test_e3044*.py' -v
cd master
python -m pytest -v tests/test_e3044_buyer_saga_db.py tests/test_swap_accept_e2041.py tests/test_e3027_seller_saga_db.py
```

Las pruebas de PostgreSQL comprueban persistencia atómica, rollback, replays,
una intención por aceptación, payload sin token y exclusión del worker E2.
