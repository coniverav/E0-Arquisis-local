# E2-045 — Evidencia del ensayo integrado local v3

**Alcance:** PostgreSQL Docker temporal `energyshark_test_e2045_qa` sin volumen persistente; broker AMQP en memoria; HTTP de wallet con `httpx.MockTransport`; firma ES256 y JWKS de prueba. No se usaron `.env` privado, broker, wallet, AWS ni MET reales.

## Referencias y límites del contrato

Los tests usan las copias locales de schemas oficiales v3.0.1 documentadas en [`master/tests/contracts/v3/PROVENANCE.md`](../../master/tests/contracts/v3/PROVENANCE.md), fijadas al commit `6cae6dcf1be954eb80c28fa34af52b1fcadf91de`. Los ejemplos JSON de partida son **sintéticos del repositorio de contracts** y se validan contra esas copias oficiales. En este checkout se validan con JSON Schema `status-statement`, `offer`, `swap-settled`, `trade-recorded` e `imbalance-settlement`; los campos y rutas de `bid` y `bid-accepted` pasan por el código real del connector y backend, pero sus schemas oficiales no están vendorizados en esa carpeta. Esta prueba no acredita validación JSON Schema exhaustiva de esos dos tipos.

El [enunciado E2](../../../Enunciado%20E2.md) fija una prueba abierta **del 19 de octubre a las 13:00 al 21 de octubre a las 23:59** y el mercado puntuable desde el 22. MET se describe allí como contraparte seller y buyer. Al 28 de octubre esa ventana programada ya terminó. No se consultó el estado de los servicios reales; no hay evidencia de que el sandbox siga operativo o pueda reabrirse. El acceso público al repositorio oficial tampoco estuvo disponible durante esta comprobación, de modo que esta corrida se apoya en el commit localmente vendorizado.

## Escenarios ejecutados

| Escenario | Recorrido y comprobaciones | Resultado |
| --- | --- | --- |
| Seller local | `status-statement` por `consume_forever()` → oferta calculada con `GET /balance` simulado → outbox `market.offer.KLD` → bid dirigido duplicado → una reserva `HOLD` y una intención de swap → `POST /swaps` responde `202` sin liquidar → `swap-proposed` permite un solo `bid-accepted` → `swap-settled` confirma reserva, snapshot y `offer-updated`. | Pasó. `HTTP 202` dejó el swap `SUBMITTED`; solo el evento lo dejó `SETTLED`. |
| Buyer local con oferta **sintética** de MET | `status-statement` deficitario → `distance-table` → oferta MET recibida por `consume_market_forever()` → candidata con precio efectivo → **POST manual** `/internal/market/bids` → bid dirigido a `city.MET` → `bid-accepted` con token ES256/JWKS de prueba → `POST /swaps/{id}/accept` responde `202` → `swap-settled` → `trade-recorded` → `market-closed` → `imbalance-settlement` → outbox `negotiation-report`. | Pasó el recorrido simulado. No prueba la generación autónoma del bid, la aceptación del reporte por la central ni interacción real con MET. |

En el escenario buyer, `minBid=500`, déficit=1000 KWH, oferta inicial de MET=1200 KWH a 661,50 CR/kWh (`imbalancePrice × 0,9`), transporte=1,00 CR/kWh y bid manual=1000 KWH. El snapshot de `swap-settled` quedó en **338.500 CR y 1000 KWH**; el hecho de transporte registró 1000 CR y la liquidación sintética de cierre informó **337.500 CR**, cobertura cero y quema de los KWH. La prueba no interpreta este dato sintético como observación de la wallet real.

### Evidencia de ledger y reporte

El test buyer consulta `Cycle`, `LedgerEntry`, `WalletBalanceSnapshot`, `TradeRecord`, `ImbalanceSettlement` y `NegotiationReport` en PostgreSQL. Confirmó que `LedgerEntry` conservó el mismo número de filas después del swap, transporte y cierre; `Cycle.budget_balance` siguió en **0,00 CR** y `Cycle.energy_balance` en **−1000 KWH**. El reporte generado se publicó solo en el broker **simulado** con `{budgetBalance: 0, energyBalance: -1000}`. Esto diverge de los hechos wallet y cierre del propio escenario. La ausencia de financiamiento v3 en el fixture limita cualquier cálculo de saldo final de producción; la falta de aplicación de estos eventos al ledger local sí queda confirmada en el recorrido probado.

### Salida conservada de los ensayos locales

La ejecución con `pytest -q -s` produjo este extracto sin credenciales ni tokens:

```text
E2-045 seller local: offer=PUBLISHED bid=RESERVED wallet_202=SUBMITTED swap=SETTLED reserva=COMMITTED offer-updated=PUBLISHED
E2-045 buyer local: offer_MET=recibida bid=PUBLISHED wallet_202=ACCEPT_SUBMITTED swap=SETTLED trade=BUYER imbalance=0 report=PUBLISHED_en_broker_simulado wallet_CR_cierre=337500.00 ledger_CR=0.00 ledger_KWH=-1000.00
2 passed in 5.08s
```

El log completo de esa corrida se guardó fuera de Git en `/tmp/energyshark-e2045-local-qa.log`; el extracto anterior permanece en este documento aunque `/tmp` se limpie.

## Pruebas ejecutadas y errores

| Comprobación | Resultado |
| --- | --- |
| Seller existente, antes de ampliar el arnés | `1 passed in 4.13s` |
| Seller + buyer integrados, corrida con log | `2 passed in 5.08s` |
| Buyer + seller en orden inverso | `2 passed in 4.86s` |
| Suites relacionadas de master: seller, feed, selección, bids, wallet, hechos y reportes | `132 passed, 1 warning in 102.00s` |
| Suite completa del connector | `142 passed, 8 subtests passed in 1.81s` |
| Proveniencia de contratos v3, wallet reconciliation y ciclo v3 | `27 passed in 18.35s` |

Durante la construcción del arnés seller/buyer hubo dos errores **de pruebas**, corregidos antes de las corridas finales: un nombre incorrecto de aserción `AsyncMock` al añadir la entrega por `market.{CODE}`, y una aserción inicial del ensayo seller que buscaba `cycleId` en el envelope de wallet (los eventos oficiales no lo llevan). No se cambió lógica de negocio. La advertencia final de master proviene de una API deprecada de `starlette.testclient`/AnyIO, sin fallo de prueba.
