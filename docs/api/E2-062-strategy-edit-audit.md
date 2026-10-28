# E2-062 — Estrategia editable sin redeploy (RF04 / AD6)

## Reutilización

La tabla `strategy_configs`, el `Cycle.strategy_version` y la asociación
transaccional en `status-statement` provienen de E2-026/AD6. El endpoint
`POST /market/strategy` y las consultas `GET /market/strategy` y
`GET /market/strategies` provienen de E2-057. **No duplicar modelos ni rutas**.

`GET /market/strategy/options` expone las políticas aceptadas y límites
básicos para construir el formulario frontend sin duplicar constantes.

## Edición protegida y efectiva por ciclo

`POST /market/strategy` exige JWT válido con `sub` auditable. Se guardan todos
los campos en **una versión nueva `PENDING`**, sin sobrescribir la anterior:

```json
{
  "offerPriceMarkup": "1.1700",
  "offerQuantityFraction": "0.8000",
  "bidAcceptancePolicy": "MAX_REMAINING",
  "offerSelectionPolicy": "MIN_DISTANCE",
  "spanSeconds": 45,
  "expectedVersion": 1
}
```

`expectedVersion` es opcional por compatibilidad con clientes E2-057. Cuando
se especifica, un valor desactualizado produce **409** con el código
`STALE_STRATEGY_VERSION`; la edición no crea ninguna versión. La versión se
comprueba bajo el mismo bloqueo transaccional que usa AD6 al abrir los ciclos.
El guardado **no requiere redeploy ni reinicio** de `master`, `master2` ni
workers: todos consultan la misma PostgreSQL.

Los decimales admiten como máximo cuatro cifras fraccionarias, coherentes con
`Numeric(10,4)`, para evitar auditorías de valores redondeados silenciosamente.
Las políticas y el `span` se validan contra las opciones admitidas por AD6.

`GET /market/strategy?cycleId=...` distingue la versión `ACTIVE`, la última
`PENDING` y la versión **realmente fijada** en el ciclo consultado. El inicio
del próximo ciclo selecciona la nueva versión según `market.opensAt` oficial;
un `status-statement` tardío no reescribe la estrategia de un ciclo anterior.

## Auditoría de cambios

`GET /market/strategy/audit?offset=0&limit=50` requiere JWT y devuelve
`total`, `offset`, `limit`, `items`, ordenados de versión más nueva a antigua.
Cada registro indica `version`, `previousVersion`, `status`, `createdAt`,
`createdBy`, `effectiveFromCycleId` y diferencias `changes` (`before`/`after`)
para los cinco parámetros. Es una vista sobre el historial persistente
versionado; **no agrega una segunda fuente de verdad ni una migración**.

Las versiones anteriores conservan sus parámetros originales. Solo evolucionan
sus campos operacionales de activación (`status`, `effective_from_cycle_id`).

## Alcance y dependencias

E2-062 cubre edición, persistencia, auditoría, validación y vigencia.
**No implementa nuevas políticas de aceptación**: `FIFO` es el comportamiento
actual del receptor y `MAX_REMAINING`/`BATCH` requieren su lógica comercial
propia de las tareas E2-063/E2-064. La selección de ofertas y el `span` ya son
consumidos desde la estrategia del ciclo en E2-037/E2-054, y los valores de
precio/cantidad desde E2-028. No declarar los tres modos de aceptación como
probados en producción solo por poder configurarlos.

## Pruebas

PostgreSQL con nombre `energyshark_test_*`, como exige `conftest.py`:

```bash
python -m pytest -q tests/test_e2062_strategy_edit.py tests/test_e2057_market_api.py tests/test_strategy_config_ad6.py --tb=short
```
