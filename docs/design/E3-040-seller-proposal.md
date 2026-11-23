# E3-040 — Propuesta seller y espera de swap-proposed

## Base y corrección de E3-039

La rama `develop` posterior a PR #116 importa `seller_reservation`, pero
el módulo y su diseño no estaban incluidos en el commit. Este patch los
incorpora para que las Lambdas puedan importarse en un despliegue limpio.

## Flujo implementado

1. AD4 y E3-038 ya confirmaron una sola reserva `HOLD`, el `SwapRecord`
   y la ejecución `SagaExecution` con una sola intención `SAGA_START`.
2. `ReserveEnergy` verifica esa reserva sin crear otra.
3. `ProposeSwapAndWait` inicia la Lambda `seller-proposal` con task token.
   `register_task_token` lo persiste y hace commit **antes** del POST.
4. `prepare_seller_proposal` bloquea el `SwapRecord` en PostgreSQL, coteja
   ejecución seller, bid, reserva, cantidades, memo y llave idempotente.
   Calcula `proposal_expires_at = min(now+55s, market_closes_at)` una sola vez;
   si el cierre es inminente o ya venció, falla sin llamar a Wallet.
5. La Lambda envía a `POST /swaps` únicamente `give`, `take`, `expiresAt`,
   `memo`. El cuerpo se construye con la intención persistida, no con el precio
   actual. Reintentos conservan **el mismo cuerpo y `Idempotency-Key`**.
6. HTTP **202** solo confirma recepción; la Lambda deja vigente el callback.
   Un replay HTTP **200/PROPOSED** con estado conocido puede completar el
   mismo task token. La confirmación `swap-proposed` normal llega por el flujo
   de evento Wallet, que debe correlacionar el token (`E3-024`).
7. `bid-accepted` **no** se publica en este paso; es responsabilidad de E3-041
   y se habilita solo tras `swap-proposed` auténtico.

## Límites y pendientes

- No se activa `E3_SELLER_SAGA_ENABLED`.
- Un vencimiento o una respuesta perdida con `swapId` incierto no libera
  el `HOLD` a ciegas. La reconciliación completa por `GET /swaps/{swapId}`
  pertenece a E3-047. `ReconcileProposal` de la ASL todavía carece de una ruta
  completa sin `swapId`; el despliegue real debe esperar ese cierre.
- Faltan E3-041/E3-042 y el cutover de los workers E2/E3.
- La presente validación no incluye invocación real de AWS ni de Wallet.

## Pruebas locales sugeridas

```bash
python -m unittest discover -s infra/cdk/tests -p 'test_e3040*.py' -v
python -m unittest discover -s infra/cdk/tests -p 'test_wallet_lambda.py' -v
cd master
python -m pytest -v tests/test_e3040_seller_proposal_db.py tests/test_e3039_seller_tasks_db.py tests/test_e3038_seller_saga_start.py
```

Usar solo `DATABASE_URL=.../energyshark_test_*` para PostgreSQL.
