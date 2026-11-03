# Post-mortem AD2: Persistencia y autoridad del ledger (E2-082)

**Revisión retrospectiva:** 3 de noviembre de 2026. Se compara la decisión de [AD2](../ADR-002-ledger-persistence.md) con la implementación E2 disponible, sin alterar el ADR de E1.

### Decisión y resultado de E1

AD2 eligió un ledger híbrido: un registro de operaciones por ciclo (`LedgerEntry`) y un balance materializado (`Cycle`). [`ledger.py`](../../../master/app/services/ledger.py) aplica cambios con bloqueo de la fila de ciclo, verifica la combinación `(idpk, operation_type)` y actualiza historial y snapshot en la transacción del llamador. También permite reconstruir y comparar saldos. La estructura es útil para historial y consultas rápidas, pero mantiene dos representaciones que pueden divergir si se aplican operaciones fuera del camino transaccional. La condición *append-only* es una regla de uso de los servicios, **no una prohibición de UPDATE/DELETE impuesta por el esquema**.

En E1 los saldos se calculaban localmente, incluyendo presupuesto arrastrado entre ciclos. La implementación se ajustó a la decisión de persistencia, pero todavía no había una autoridad externa contra la cual contrastar ese resultado.

### Cambio real al protocolo v3

En E2 la **wallet es la autoridad económica** de `CR` y `KWH` disponibles y retenidos. El ledger local conserva el valor de historial y explicación, pero no define el saldo oficial. El código actual introduce `SwapRecord`, snapshots de wallet y asientos del statement ([`models.py`](../../../master/app/models.py)). [`wallet_service.py`](../../../master/app/services/wallet_service.py) correlaciona estados de swaps; [`cycle_accounting.py`](../../../master/app/services/cycle_accounting.py) aplica `SWAP_SETTLED`, transporte del comprador e imbalance como efectos diferenciados; [`trade_events.py`](../../../master/app/services/trade_events.py) deduplica `trade-recorded` por `swapId` e `imbalance-settlement` por `settlementId`.

[`wallet_reconciliation.py`](../../../master/app/services/wallet_reconciliation.py) consulta `GET /balance` y `GET /statement`, conserva `entryId`, referencia y fechas, y contrasta asientos oficiales, efectos locales y materialización. Las diferencias no se cierran creando asientos inventados. El resultado distingue `MATCH`, `EXPLAINED`, `DIVERGENT` y `NO_LOCAL_BASELINE`; la evidencia por operación también puede quedar `PENDING_WALLET`, `PENDING_LOCAL` o `WALLET_ONLY`. Una respuesta `202` no equivale a un intercambio confirmado.

### Evidencia disponible

- [`test_ledger.py`](../../../master/tests/test_ledger.py), [`test_idempotency.py`](../../../master/tests/test_idempotency.py): mecanismos de E1 para secuencia de asientos y duplicados.
- [`test_wallet_and_cycle_v3.py`](../../../master/tests/test_wallet_and_cycle_v3.py), [`test_e2051_cycle_accounting.py`](../../../master/tests/test_e2051_cycle_accounting.py): hechos económicos de v3, roles y aplicación del ledger.
- [`test_e2070_reconciliation.py`](../../../master/tests/test_e2070_reconciliation.py): diferencias entre ledger y wallet aunque coincidan snapshots, correlación con `entryId`/`swapId`, lectura incremental, asientos tardíos y reportes históricos.

Las pruebas existen en el repositorio, pero no se adjunta su salida de ejecución ni una conciliación completa de un cierre oficial. El paquete tampoco contiene una serie comparativa de balances oficiales y locales tomada durante varios ciclos reales.

### Tradeoff y divergencias

**Se conserva el ledger híbrido como proyección auditable, no como fuente de verdad.** La solución incorpora estados pendientes y conciliación asíncrona; se paga el costo de mantener información duplicada y consultar la wallet a cambio de explicar las operaciones y detectar diferencias. El balance local puede ir retrasado respecto de wallet; ese desfase debe quedar registrado y no ocultarse con un `MATCH` basado solo en snapshots.

El cambio más importante frente al diseño E1 es la **autoridad de los saldos**. La semántica de la energía al cierre y de los `CR` retenidos no puede derivarse simplemente del cálculo inicial `generationCapacity - consumption`. Otra limitación explícita: algunos hechos se persisten aunque no exista el `Cycle` local, y en ese caso el efecto económico puede quedar sin entrada en el ledger hasta que se investigue o recupere el contexto. La conciliación detecta ese estado, pero no lo corrige de manera silenciosa.

### Cierre y acciones pendientes

AD2 queda **reutilizada con autoridad económica modificada** conforme a AD5. Existe implementación de conciliación y cobertura de pruebas en el código, pero para comprobar el resultado exigido por E2 aún hace falta adjuntar una ejecución frente a `GET /balance` y `/statement` de wallet central al cierre de un ciclo, con explicación de diferencias y saldos disponibles/retenidos.
