# E3-046: aceptación del swap comprador con intentos durables

La E3-044 persiste `SwapRecord`/`SagaExecution`/`SAGA_START` en el ingreso
`bid-accepted`; E3-045 verifica ES256/JWKS, claims y deadline sin exponer JWT.

La tarea `buyer-acceptance` de E3-046:

1. Corrobora en PostgreSQL la identidad `sagaExecutionId` = `idpk` del
   `SwapRecord` = `accept_idempotency_key`, el `swapId`, el identificador de
   registro, el ciclo y el `MarketBid` de compra. Si no coinciden, falla antes
   de persistir o reemplazar un task token.
2. Registra el task token de espera `TERMINAL` antes del HTTP. Para el
   `waitForTaskToken` de 30 segundos limita su vencimiento local a 29
   segundos, sin exceder el plazo absoluto de la saga.
3. Bajo `SELECT ... FOR UPDATE OF sr`, comprueba nuevamente vencimiento,
   guarda `ACCEPT_SUBMITTED`, contador, hora de intento, error `WALLET_IN_FLIGHT`
   y una protección breve frente a dos POST simultáneos, todo antes del HTTP.
4. Llama únicamente a `POST /swaps/{swapId}/accept` con el
   `accept_idempotency_key` persistido. Un retry reutiliza esa misma llave.
5. Registra respuesta/error tras HTTP sin sobrescribir `SETTLED`, `REJECTED`,
   `EXPIRED` ni `CANCELLED` si el evento Wallet fue más rápido.

**Un HTTP 202 es recepción, no liquidación.** Un HTTP 200 con un estado
terminal tampoco aplica ledger ni completa el callback anticipadamente:
se espera el evento Wallet persistido o una reconciliación autorizada.
`swapToken`, task tokens y credenciales no salen en outputs de Step Functions.

Las escrituras económicas del comprador siguen en `WalletService` al
persistir el evento Wallet. La correlación de callbacks y resolución de
reentregas/orden invertido se completará en E3-047; sin ella no se habilita
`E3_BUYER_SAGA_ENABLED` en producción.

## Pruebas

- Offline: `python -m unittest discover -s infra/cdk/tests -p 'test_e3046*.py' -v`.
- PostgreSQL: `cd master && python -m pytest -v tests/test_e3046_buyer_acceptance_db.py tests/test_e3045_buyer_validation_db.py tests/test_e3044_buyer_saga_db.py`.
- Regresión de Lambda: `python -m unittest discover -s infra/cdk/tests -p 'test_wallet_lambda.py' -v`.
