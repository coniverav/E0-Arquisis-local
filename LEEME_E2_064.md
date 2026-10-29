# E2-064 — Política de selección por precio efectivo y rutas (RF03 / AD6)

## Alcance

Integra la política de compra del ciclo con los servicios existentes:

- **E2-036:** `transport_pricing.calculate_transport`: costo por kWh con distancia en metros convertida a km, redondeo monetario y costo oficial punitivo para rutas `enabled=false`.
- **E2-037:** `offer_selection.get_buy_candidates`: orden por `CHEAPEST_EFFECTIVE_PRICE` o `MIN_DISTANCE`, sin inventar distancias/tarifas, descartando ofertas inciertas, retiradas, sin energía suficiente o sin información de ruta.
- **E2-062 / AD6:** lee `Cycle.strategy_version` y conserva política y `span` de ese ciclo, aun si se guarda otra versión `PENDING`.
- **E2-038:** reutiliza `enqueue_buy_bid` para validación final del `minBid`, disponibilidad y deadlines; el bid se guarda en PostgreSQL y en la outbox de forma durable, sin incluir precio en el mensaje v3.
- **E2-054:** elimina el corte artificial `effectivePricePerEnergy > market.imbalancePrice` y el fallback que deducía el costo de un bid pendiente desde ese límite.

**No introduce un cap E1, una tarifa inventada, modificaciones en el esquema, ni migraciones nuevas.** Sigue limitado por el presupuesto informado por Wallet (`cr_available + credit_line`), cantidad ofertada, `minBid`, déficit, deadline oficial, validez de la ruta y cantidad máxima de bids por tick.

## Política vigente por ciclo

Usar `POST /market/strategy` autenticado para crear una nueva versión `PENDING` (E2-062); se activa cuando abre el siguiente ciclo, no durante el actual.

Ejemplos de `offerSelectionPolicy`:

- `CHEAPEST_EFFECTIVE_PRICE`: primero menor **precio nominal + costo oficial de transporte por kWh**, con desempates estables.
- `MIN_DISTANCE`: primero menor distancia, aun si el precio efectivo es mayor; desempata por precio efectivo.

El campo `spanSeconds` retrasa el inicio de compras hasta `market.opensAt + spanSeconds`. `lastBidAt` y `closesAt` siguen siendo límites obligatorios: si el span los supera, no se compra.

La ruta deshabilitada **no significa ruta imposible**: sigue siendo elegible si el mensaje `distance-table` contiene `disabledRouteTransportCost`. Si falta el costo oficial, esa oferta no tiene precio efectivo verificable y queda fuera.

## Conservadurismo de Wallet con bids pendientes

El código actual no guarda una cotización inmutable del precio efectivo del bid enviado. Por eso, E2-064 **no recalcula su precio usando el feed mutable**, que podría haber cambiado. Si hay bids salientes con estado `PENDING` del mismo ciclo, no hace nuevas compras en el siguiente tick hasta que se resuelvan. Esto evita presupuestar el compromiso anterior con una cifra especulativa. En un mismo tick puede generar varios bids, contabilizando el precio efectivo de cada uno y limitando la cantidad por saldo y `minBid`.

El saldo Wallet sigue siendo la fuente de verdad; el comprador no modifica balances económicos por crear un bid. E2-064 no introduce confirmaciones Wallet ni cambios en el flujo de swaps.

## Aplicación

Desde la **raíz del repositorio** (con E2-036/037/038/054/062 ya integradas):

```bash
# Crear rama antes de modificar archivos
git switch -c feat/e2-064-buyer-policy

# Extraer el ZIP E2-064 aquí y ejecutar
python3 aplicar_e2_064.py
git diff --stat
git diff --check
```

El instalador crea `master/tests/test_e2064_buyer_policy.py`, modifica únicamente los segmentos pertinentes de `master/app/services/autonomous_buy.py` y actualiza las expectativas de `master/tests/test_e2054_autonomous_buy.py` para eliminar la prueba del cap antiguo. Guarda los originales en `.e2064_backup/` sin sobrescribir respaldos previos. Agrega `.e2064_backup/` a `.gitignore` o evita incluirlo en el commit. Si el código base difiere, se detiene sin escribir cambios.

## Pruebas

```bash
chmod +x probar_E2_064.sh
SOLO_E2064=1 ./probar_E2_064.sh
# Posteriormente, suite completa:
./probar_E2_064.sh
```

El script construye `migrate`, levanta `db`, **elimina y recrea solamente** `energyshark_test_e2064`, aplica las migraciones y ejecuta los tests nuevos y de E2-036/037/038/054/062. El modo normal también corre toda la suite. Se detiene si Alembic tiene múltiples `heads`.

Tests cubiertos: precio superior a `imbalancePrice` o ausencia de ese campo, orden por precio efectivo y ruta, elección por menor distancia, ruta deshabilitada con/sin penalización, capacidad Wallet y minBid, span/versionado, último momento de bid, cierre de mercado, y prevención de compras adicionales con PENDING sin cotización persistida.

## Validación y compatibilidad

- No hacer `git add .` antes de revisar respaldos: `git add master/app/services/autonomous_buy.py master/tests/test_e2054_autonomous_buy.py master/tests/test_e2064_buyer_policy.py`.
- Si existe otro cambio previo en esos archivos, revisar `git diff` antes de integrar; no resolver conflictos sobreescribiendo pruebas a ciegas.
- El script realiza validación de sintaxis y es repetible; **no sustituye** la ejecución Docker/PostgreSQL en tu repositorio.
