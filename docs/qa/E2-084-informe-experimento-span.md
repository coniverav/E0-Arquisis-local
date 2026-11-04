# E2-084 — Informe del experimento de span (RF06 / RDOC05)

## 1. Objetivo y procedencia

El objetivo es describir cómo se comportó la compra de energía con distintos tiempos de espera (`spanSeconds`) y qué puede concluirse sobre cobertura y costo frente al baseline de no participar. La comparación es **observacional**: cada ciclo ocurre en un mercado distinto, con oferta, rutas, precios y disponibilidad de Wallet potencialmente diferentes.

Los agregados de producción de la sección 3 fueron **comunicados por el equipo para esta tarjeta**. En el workspace no hay una exportación por `cycleId` que permita recomputarlos: `datos_e2066/` y `datos_e2067/` contienen solo sus archivos `.gitignore`. No se consultó producción para elaborar este informe. Tampoco se conoce el intervalo exacto de recolección ni el commit desplegado durante cada grupo de ciclos.

La definición funcional y los criterios de registro provienen del enunciado E2 (RF04, RF06 y RDOC05), del [plan E2-066](E2-066-plan-spans.md), del [protocolo E2-067](E2-067-ejecucion-registro.md) y de las implementaciones citadas abajo. Los escenarios de `qa/e2066/escenarios_sinteticos.json` son simulaciones y **no forman parte de los resultados productivos**.

## 2. Metodología y métricas

- **Configuración aplicada.** E2-073 permite editar `spanSeconds` y las políticas sin redeploy desde `Arquisis-G10-Frontend/src/components/MarketStrategyView.jsx`. `master/app/services/strategy_config.py` crea una versión pendiente y fija la versión efectiva al abrir cada ciclo; `master/app/services/offer_selection.py` permite considerar compras desde `market.opensAt + spanSeconds`, siempre antes de `lastBidAt`. Para comprobar el span efectivamente usado hay que revisar `strategyVersion` y `spanSeconds` de cada ciclo, no solo la configuración visible al momento de la consulta.
- **Diseño previsto.** E2-066 propuso cinco valores (0, 30, 60, 120 y 240 s), bloques rotados y constancia de las otras políticas. Los datos comunicados corresponden a **20, 60 y 120 s**, con tamaños de grupo muy distintos. No hay evidencia de ejecución de los bloques, constancia de políticas ni razón documentada para reemplazar los valores previstos. `qa/e2066/comun.py` acepta solo los cinco spans del plan original, por lo que ese plan no puede reproducir directamente el grupo de 20 s sin adaptar la herramienta. La configuración de aplicación sí admite 20 s.
- **Fuente de las métricas.** `master/app/services/cycle_metrics.py` cuenta compras desde asientos `SWAP_SETTLED` del ledger, no desde bids ni respuestas HTTP `202`. Registra `deficitKwh`, `boughtKwh`, `coveredKwh`, `coverageRate`, bids rechazados, costos de compra y transporte, costo de liquidación, ingreso de venta, `baselineCost`, `realCost` y `savingsVsBaseline`. La API `GET /market/cycle-metrics/compare` (`master/app/routers/cycle_metrics_api.py`) expone el detalle paginado.
- **Definiciones económicas.** El enunciado define `baseline(c) = déficit × imbalancePrice − sellable × buybackPrice`, `real(c) = compras + transporte + desbalance + multas − ventas − recompra` y `ahorro(c) = baseline(c) − real(c)`. La métrica local calcula `realCost` solo en estado `FINAL`, después de liquidación y de los cargos de transporte de compras confirmadas. `coveredKwh = min(deficitKwh, boughtKwh)`: los kWh comprados **no equivalen necesariamente** a cobertura de déficit.
- **Criterio de comparación.** El plan E2-066 y `qa/e2067/consolidar.py` requieren, entre otros controles, métrica `FINAL`, baseline de fuente `SETTLEMENT`, costo real conocido, déficit positivo y span/políticas efectivamente aplicados. Un ciclo `FINAL` por sí solo no garantiza que satisfaga todos esos filtros. Sin filas por ciclo no se puede determinar cuántos ciclos son comparables económicamente.

## 3. Resultados productivos comunicados

| Span | Ciclos registrados | Ciclos `FINAL` | Ciclos con compras | Energía comprada | Ahorro acumulado comunicado |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 20 s | 134 | 124 | 0 | 0 kWh según «sin compras» | No informado |
| 60 s | 119 | 103 | 15 | 109.624 kWh | −7.004.796,58 CR |
| 120 s | 6 | 0 | No informado | No informado | No calculable con ciclos `FINAL` |

En total se comunicaron **259 ciclos**, de los cuales **227** figuran como `FINAL` (suma de los grupos). La proporción `FINAL` es 124/134 para 20 s, 103/119 para 60 s y 0/6 para 120 s; estas proporciones describen cierre y disponibilidad de métricas, **no** éxito comercial. No se proporcionaron `cycleId` individuales, versiones de estrategia ni lista de exclusiones.

| Span | Déficit cubierto y tasa de cobertura | `baselineCost` agregado | `realCost` agregado | Latencia Wallet atribuible |
| ---: | --- | ---: | ---: | --- |
| 20 s | No informados | No informado | No informado | Sin mediciones disponibles |
| 60 s | No informados; `boughtKwh` no basta para calcularlos | No informado | No informado | Sin mediciones disponibles |
| 120 s | Sin ciclos `FINAL` comparables | No disponible | No disponible | Sin mediciones disponibles |

Los 109.624 kWh del grupo de 60 s fueron comunicados como energía comprada; no hay desglose `boughtKwh` por ciclo para comprobar su suma. Faltan los déficits por ciclo para calcular `coveredKwh` y `coverageRate`, y faltan bids rechazados por span. La ausencia de compras reportada para 20 s no implica costo real cero: puede haber desbalance o recompra al cierre. Para 120 s no hay ciclos `FINAL`; sus seis observaciones no deben entrar en una comparación de costos finales.

El ahorro negativo comunicado para 60 s indica, **si se calculó como `baselineCost − realCost` sobre el mismo conjunto de ciclos**, que el costo real agregado superó el baseline en 7.004.796,58 CR. No se entregaron las sumas separadas de `baselineCost` y `realCost`, su `baselineSource`, ni el subconjunto exacto usado para ese agregado. Por ello no se atribuye esa diferencia al span ni se calcula una tasa de ahorro o un costo medio a partir del número de ciclos registrados.

La fórmula de desempeño del enunciado incluye multas. El cálculo E2-065 de `realCost` suma compras, transporte y liquidación y resta ingresos de venta; no agrega por separado asientos `PENALTY`. Antes de equiparar el agregado local con el puntaje oficial, hay que verificar si hubo multas y cómo se reflejaron en Wallet y en el ledger de esos ciclos.

## 4. Wallet y condiciones externas

El equipo reportó problemas de disponibilidad de Wallet durante parte del experimento. No hay en el workspace una serie temporal del incidente, conteos de timeouts/HTTP 429 ni medidas de latencia que permitan asignarlo a los grupos de 20, 60 o 120 s. Tampoco se dispone aquí de `GET /statement`, estados de swaps o conciliaciones por ciclo para comprobar independientemente los importes comunicados. El HTTP `202` confirma recepción de una escritura; la compra económica requiere `swap-settled` y sus asientos confirmados.

La instrumentación existe: `master/app/services/wallet_observability.py` registra `latencyMs`, endpoint y resultado; `qa/e2067/latencias.py` calcula media, p50, p95, máximo, timeouts y 429 cuando las llamadas se pueden atribuir por `cycleId` o `idpk`. **No se reporta un valor de latencia para este experimento** porque no se proporcionaron logs o exportaciones correlacionables. Atribuir llamadas solo por proximidad horaria sería inseguro si se solaparon ciclos.

Las decisiones de otras ciudades pueden cambiar la liquidez, el precio efectivo, la hora de aparición o retiro de ofertas y la probabilidad de que un bid sea aceptado. También influyen `distance-table`, rutas deshabilitadas, cierres del mercado y las ventanas oficiales del ciclo; `master/app/services/offer_selection.py` descarta ofertas inciertas o desactualizadas al seleccionar candidatas. E2-067 pide capturas tempranas y finales del libro, pero no hay capturas productivas por ciclo en este workspace. Estos son **factores de confusión conocidos por el protocolo**, no incidentes concretos demostrados durante los 259 ciclos.

## 5. Hallazgos, límites y conclusión

1. Hay evidencia agregada comunicada de compras únicamente en el grupo de 60 s; el grupo de 20 s no registró compras y el de 120 s no tiene cierres `FINAL`. Esto describe la ejecución observada, sin demostrar que esperar 60 s causó las compras ni que 20 s las impidió.
2. El ahorro acumulado negativo comunicado para 60 s requiere el desglose `baselineCost`, `realCost`, `baselineSource`, déficit y liquidaciones por ciclo antes de interpretarlo como desempeño económico comparable. Los `FINAL` reportados no sustituyen esa comprobación.
3. El diseño de bloques E2-066 no coincide con los spans ni con la distribución observada. La indisponibilidad de Wallet y las decisiones no controladas de otras ciudades limitan todavía más la comparación. **No hay base suficiente para declarar superior a ninguno de los spans.**

Para cerrar la trazabilidad de RDOC05, el equipo debe conservar una exportación sanitizada de `/market/cycle-metrics/compare` con `cycleId`, `strategyVersion`, `spanSeconds`, estado y componentes de costo; registrar las capturas y factores externos por ciclo previstos en E2-067; y adjuntar telemetría Wallet correlacionada para medir latencias y disponibilidad por grupo. Después se podrá recalcular el ahorro solo sobre ciclos elegibles, explicar exclusiones y revisar si las condiciones de mercado permiten alguna comparación más estrecha. Este informe no incorpora cifras ausentes como si fueran ceros.
