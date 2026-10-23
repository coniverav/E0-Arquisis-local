# Post-mortem AD2: Persistencia y autoridad del ledger

### Contexto y decisión de E1

AD2 buscaba mantener balances consultables y explicar cada cambio de energía y presupuesto. Se eligió un **ledger híbrido** sobre PostgreSQL: el código agrega entradas por movimiento y mantiene un estado materializado por ciclo para consultas rápidas. En E1, el nodo local calculaba esos saldos, incluido el arrastre de presupuesto. El ADR describe un historial *append-only*; el modelo no impone por sí mismo una prohibición de actualizar o borrar entradas.

El tradeoff aceptado fue mantener dos representaciones del mismo estado y coordinarlas transaccionalmente a cambio de consultas eficientes y trazabilidad histórica.

### Qué ocurrió al implementar E1

`E0-Arquisis-local/master/app/models.py:57-231` define `Cycle` y `LedgerEntry`; `E0-Arquisis-local/master/app/services/ledger.py:9-127` aplica efectos, reconstruye saldos y compara el resultado con el snapshot. `E0-Arquisis-local/master/app/services/budget_carryover.py:19-45` agrega ajustes de presupuesto entre ciclos sin arrastrar energía. `E0-Arquisis-local/master/app/routers/cycles.py:120-224` y el frontend exponen el historial. Esto respalda el diseño híbrido en E1, aunque sus pruebas locales no equivalen a una conciliación con una autoridad externa.

### Qué cambió con el protocolo v3

En E2, la **wallet es la fuente de verdad** de `CR` y `KWH`: saldos `available` y `held`, línea de crédito, retenciones, swaps y liquidaciones. Los créditos se arrastran, pero toda la energía de la wallet se cierra a cero entre ciclos. El ledger local deja de poder afirmar por sí mismo cuál es el saldo oficial.

Además, `POST /swaps` y sus operaciones relacionadas responden de forma asíncrona: un HTTP 202 no prueba liquidación. E2 exige tolerar eventos duplicados y el orden variable entre `swap-settled` y `trade-recorded` (AD5). El nodo deberá procesar también `imbalance-settlement` y contrastar sus registros con `GET /balance` y, cuando corresponda, `GET /statement`. Nada de esto está implementado todavía en el checkout E1.

El estado actual **todavía trata sus saldos calculados localmente como autoridad**: `E0-Arquisis-local/master/app/routers/internal_messages.py:236-247` inicia la energía con `generationCapacity - consumption`, y `E0-Arquisis-local/master/app/services/negotiation_report.py:52-66` reporta saldos reconstruidos de ese ledger. No hay cliente wallet, modelo de swaps ni conciliación con saldos oficiales. Esa posición energética neta E1 no equivale a los `KWH available` de la wallet v3.

### Reevaluación de la decisión

**Se conserva el patrón de historial de movimientos y proyección materializada**, útil para RF01, auditoría y explicación de resultados. La inmutabilidad estricta del historial sería una garantía adicional por definir y verificar.

**Para E2 debe modificarse el supuesto de autoridad:** se propone que el ledger local sea una proyección auditable de los hechos observados de la wallet y de la central, sin calcular por sí solo el saldo oficial. La adaptación deberá distinguir estados pendientes, retenidos y liquidados; aplicar cada efecto una sola vez; relacionar `swap-settled` con `trade-recorded`; y registrar diferencias con los saldos oficiales sin sobrescribir silenciosamente el historial.

La decisión original no se reemplaza retroactivamente: el mecanismo y la medida de conciliación deberán definirse en **AD5**, usando `GET /balance` y, cuando corresponda, los asientos de `GET /statement`.

### Tradeoff y lecciones

La redundancia elegida en E1 permite reconstruir y explicar operaciones, pero E2 añade la referencia oficial de la wallet. La proyección local podría quedar temporalmente desfasada; AD5 debe definir cómo detectar, explicar y cerrar esa diferencia. Ni la consistencia eventual ni la convergencia con wallet están demostradas por el código E1.

### Acciones y evidencia pendientes

- Implementar un cliente wallet, estados de operaciones y efectos idempotentes de `swap-settled`, `trade-recorded` e `imbalance-settlement`.
- Usar `GET /balance` y `GET /statement` para reconciliar y explicar diferencias, especialmente al cierre de cada ciclo.
- Adaptar el historial y el `negotiation-report` al nuevo estado económico, respetando la ventana de reporte.
- Probar eventos repetidos/desordenados y divergencias wallet–dashboard sin doble aplicación ni correcciones opacas.

### Conclusión

**AD2 fue útil como decisión de persistencia, pero su premisa de autoridad local deja de ser válida para E2.** Las entradas y snapshots E1 son reutilizables como base; subordinarlos a los saldos oficiales, conciliarlos y precisar su nueva semántica sigue pendiente de AD5.
