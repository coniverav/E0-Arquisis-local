# E2-046 — Evidencia local de deduplicación feed, bid y replay wallet

## Escenarios y resultado

| Recorrido | Reenvíos probados | Efecto persistido |
| --- | --- | --- |
| Feed de `market.{CODE}` → backend | `offer` con la misma `idpk`, distinto `msgId` y `timestamp`; misma `idpk` con cantidad distinta. | Una fila `MarketFeedOffer` y un claim `MarketEventClaim`. El replay quedó `DUPLICATE`; el cambio de cantidad, `NOT_PROCESSED`. Se conservó el snapshot inicial. |
| Seller: `status-statement` → oferta → bid dirigido → reserva → `POST /swaps` → `swap-proposed` → `bid-accepted` → `swap-settled` | Bid con igual `idpk` y distinto `msgId`, antes y después del settlement; bid con cantidad conflictiva; `swap-proposed` con igual `idpk` y distinto `msgId`; `swap-settled` con igual `idpk` y distinto `msgId` y, además, otro envelope completo para el mismo `swapId`. | Un `MarketBid`, una reserva `HOLD` que pasó a `COMMITTED`, un `SwapRecord` `SETTLED`, un snapshot wallet `EVENT`, un `bid-accepted`, un `offer-updated` y un solo `POST /swaps`. La oferta descontó la cantidad una sola vez. HTTP `202` mantuvo `SUBMITTED`/`HOLD`; solo `swap-settled` liquidó. El número de filas `LedgerEntry` no cambió por los replays. |
| Buyer: oferta sintética de MET → bid manual → `bid-accepted` → `POST /swaps/{id}/accept` → `swap-settled` | `bid-accepted` con igual `idpk` y distinto `msgId`; settlement con igual `idpk` y distinto `msgId` y otro envelope para el mismo `swapId`. | Una aceptación HTTP, un `SwapRecord` `SETTLED` y un snapshot wallet `EVENT`. Las recepciones de la misma `idpk` se registraron como `PROCESSED` y `DUPLICATE`. |
| Buyer: `trade-recorded` e `imbalance-settlement` | Misma `idpk` con `msgId` nuevo y envelope nuevo para el mismo `swapId` o `settlementId`. | Un `TradeRecord` y un `ImbalanceSettlement`; el ledger E1 del ciclo no recibió cargos adicionales. |

Cada entrega pasó por `LocalBroker.deliver_city()` o `deliver_market()` del ensayo E2-045, que ejecutan los consumidores reales del connector y los endpoints reales de master. Las filas `InboundMessage` conservan **todas** las recepciones como evidencia de auditoría; su multiplicidad no representa un segundo efecto económico. Las aserciones comprueban cantidades, estado, claves de idempotencia, outbox, snapshots y conteos en PostgreSQL después del commit de cada operación. Los ejemplos `status-statement`, `offer` y `swap-settled` se validaron con las copias de schemas v3 cuya procedencia consta en [`master/tests/contracts/v3/PROVENANCE.md`](../../master/tests/contracts/v3/PROVENANCE.md); `bid` y `bid-accepted` recorrieron la validación del connector y backend, sin validación JSON Schema vendorizada.

## Ejecuciones

Con `DATABASE_URL=postgresql+psycopg://energyshark_test:energyshark_test@127.0.0.1:<puerto>/energyshark_test_e2046_qa`, desde `master/`:

```text
pytest -q -s tests/test_e2046_dedup_replay.py
2 passed in 5.39s

pytest -q -s tests/test_e2046_dedup_replay.py tests/test_e2045_seller_cycle.py tests/test_e2045_buyer_cycle.py
4 passed in 8.18s

pytest -q tests/test_market_feed_persistence.py tests/test_incoming_bids.py tests/test_seller_bid_acceptance.py tests/test_seller_bid_accepted.py tests/test_wallet_events_v3.py tests/test_bid_responses.py tests/test_buy_bids.py tests/test_swap_accept_e2041.py tests/test_trade_events_e2043.py tests/test_seller_swap_dispatch.py tests/test_wallet_reconciliation_e2044.py
110 passed, 1 warning in 84.17s
```

Salida breve sin tokens ni credenciales de la última corrida integrada:

```text
E2-046 feed: 1 snapshot, 1 claim; replay=DUPLICATE; conflicto=NOT_PROCESSED
E2-046 seller: 1 bid, 1 reserva, 1 swap, 1 POST wallet, 1 liquidación;
replay con msgId nuevo y envelope nuevo: sin efecto económico adicional
4 passed in 8.18s
```

No hubo pruebas fallidas en las corridas finales. La advertencia de regresión es una deprecación de `starlette.testclient`/AnyIO, sin fallo de prueba. No se cambió lógica de negocio.

## Alcance del criterio de aceptación y pendientes

**Cumple en el entorno local simulado:** una misma `idpk` con otro `msgId` no añadió filas al ledger ni creó otro swap, reserva o settlement en los casos seller y buyer probados. También se verificó la barrera adicional por `swapId`/`settlementId` cuando llega un envelope nuevo. La prueba no acredita comportamiento de un broker o wallet reales, ni estrés concurrente de varias réplicas, reconexión con caída durante el commit o consistencia económica final de E2 en producción.

El ensayo buyer mantiene el hallazgo de E2-045: `trade-recorded` e `imbalance-settlement` se persisten como hechos, pero el ledger y `negotiation-report` E1 todavía no reflejan los saldos wallet del escenario. Es una brecha de integración económica ya identificada, no una duplicación causada por replay. La creación automática del bid buyer y la validación contra MET real también siguen fuera de este ensayo.
