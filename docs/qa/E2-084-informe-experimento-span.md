# E2-084 — Informe del experimento de span

## Objetivo y diseño observado

El `span` es la espera, en segundos desde `market.opensAt`, antes de considerar compras. El objetivo fue observar si cambiar esa espera se relacionaba con la cobertura del déficit y con el costo frente a no participar en el mercado. Originalmente se proponía los valores 0, 30, 60, 120 y 240 s en bloques rotados, manteniendo constantes las demás políticas. La ejecución documentada utilizó **60, 120 y 20 s**, de manera secuencial y con tamaños de muestra desiguales. Por tanto, el análisis es **observacional**.

La hipótesis exploratoria del plan era que esperar podía permitir observar ofertas con mejor precio efectivo, a costa de perder liquidez o superar el plazo de bids. La hipótesis descriptiva de referencia era que no aparecieran diferencias consistentes en cobertura, costo normalizado o rechazos. El corte disponible permite describir resultados, pero no aislar cuál de esos mecanismos predominó.

La política de selección registrada fue `CHEAPEST_EFFECTIVE_PRICE` y la de aceptación de bids, `FIFO`. La estrategia se guarda por versión y se vincula al ciclo según su apertura oficial, un cambio queda pendiente para el siguiente ciclo. El servicio [`strategy_config.py`](../../master/app/services/strategy_config.py) conserva la versión efectiva por ciclo, [`offer_selection.py`](../../master/app/services/offer_selection.py) calcula la primera oportunidad de compra como `opensAt + spanSeconds` y detiene los bids al llegar `lastBidAt`. La interfaz permite editar esos parámetros sin redeploy. Los valores de precio y cantidad de oferta de cada versión no forman parte del agregado recibido y no se consideran constantes por inferencia.

| Configuración observada | Versión | Período documentado | Selección / aceptación |
| --- | ---: | --- | --- |
| 60 s | v1 | Última compra: 1 nov., 02:45:33; no se informó el inicio del grupo | `CHEAPEST_EFFECTIVE_PRICE` / `FIFO` |
| 120 s | v2 | 2 nov. | `CHEAPEST_EFFECTIVE_PRICE` / `FIFO` |
| 20 s | v4 | 2–4 nov. | `CHEAPEST_EFFECTIVE_PRICE` / `FIFO` |

La versión v3 no aparece entre las tres configuraciones evaluadas en este corte; no se le asignan ciclos ni resultados.

## Métricas y criterios de lectura

La tabla `cycle_strategy_metrics` conserva por `cycle_id` la versión y el span efectivos, políticas, déficit, compras, cobertura, bids rechazados, componentes de costo, baseline, ahorro y estado. La [implementación de métricas](../../master/app/services/cycle_metrics.py) cuenta `boughtKwh` desde asientos de comprador `SWAP_SETTLED` del ledger, no desde bids aceptados ni respuestas HTTP 202. Calcula `coveredKwh = min(deficitKwh, boughtKwh)` y `coverageRate = coveredKwh / deficitKwh` cuando hay déficit; `rejectedBids` cuenta bids salientes rechazados. La [API de comparación](../../master/app/routers/cycle_metrics_api.py) expone esas filas paginadas.

El costo real local se construye como `buyCost + transportCost + settlementCost - saleRevenue`, y el ahorro como `baselineCost - realCost`. `realCost` solo se llena cuando el ciclo está `FINAL`: el cierre debe haber ocurrido, deben existir el evento y asiento de `imbalance-settlement`, y cada compra debe tener su cargo de transporte. Por eso un ciclo puede registrar una compra `SWAP_SETTLED` y seguir `AWAITING_SETTLEMENT`: en ese estado espera **la liquidación de desbalance del ciclo**, no necesariamente la liquidación del swap. Sus compras cuentan en energía comprada, pero sus costos finales y ahorros aún no entran en la comparación económica.

El baseline usa la tarifa recibida en `imbalance-settlement` para ciclos deficitarios cuando está disponible (`baselineSource = SETTLEMENT`); en otros casos puede usar `imbalancePrice` del `status-statement` (`STATUS_ESTIMATE`). El primer origen permite contrastar con la tarifa oficial de cierre; el segundo es una estimación y puede diferir del resultado final. El agregado suministrado reúne ciclos `FINAL` con costos completos, **sin desglose por `baselineSource`**. No equivale automáticamente al subconjunto más estricto previsto por el plan E2-066, que exige baseline confirmado. La fórmula oficial de desempeño incluye multas; el cálculo local citado no añade por separado asientos `PENALTY`. Antes de equiparar estos totales con el puntaje oficial habría que comprobar si los hubo y cómo quedaron reflejados.

## Resultados operacionales por span

| Span | Ciclos | `FINAL` | `AWAITING_SETTLEMENT` | `FINAL` con costos completos | Ciclos con compras | Energía comprada |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 20 s | 134 | 124 | 10 | 124 | 0 | 0 kWh |
| 60 s | 119 | 103 | 16 | 103 | 15 | 109.624 kWh |
| 120 s | 6 | 0 | 6 | 0 | 0 | 0 kWh |
| **Total** | **259** | **227** | **32** | **227** | **15** | **109.624 kWh** |

Las proporciones de ciclos `FINAL` fueron 92,5 % para 20 s, 86,6 % para 60 s y 0 % para 120 s (cálculo a partir de los conteos anteriores). Son proporciones de cierre de la métrica, no tasas de éxito comercial. De los **15 ciclos con compras de 60 s, nueve** eran `FINAL` y **seis** seguían `AWAITING_SETTLEMENT`. Los 109.624 kWh mezclan ambos estados; el corte proporcionado no separa cuánto corresponde a los nueve ciclos finales.

| Caso observado dentro del corte | Número de ciclos | Lectura admisible |
| --- | ---: | --- |
| 20 s, `FINAL`, sin compras | 124 | Costo final calculado; no hubo compra registrada. |
| 60 s, con compras y `FINAL` | 9 | Compras confirmadas en ledger y costo final disponible. |
| 60 s, con compras y `AWAITING_SETTLEMENT` | 6 | Compras registradas; falta el costo final del ciclo. |
| 120 s, `AWAITING_SETTLEMENT`, sin compras | 6 | No hay costo final comparable en este corte. |

Esta tabla describe **subconjuntos reales**, no identifica filas concretas: el resumen entregado no incluye `cycle_id` individuales. Los campos `deficit_kwh`, `covered_kwh`, `coverage_rate` y `rejected_bids` sí existen en la tabla productiva, pero **sus agregados y valores por ciclo no se incluyeron en este corte**. Por eso no se calcula una tasa de cobertura, costo por kWh deficitario ni promedio de rechazos. Se pueden extraer posteriormente sin tratar los valores pendientes como ceros, conforme al [protocolo de registro](E2-067-ejecucion-registro.md).

## Resultados económicos de ciclos `FINAL`

Los siguientes montos son sumas de los ciclos `FINAL` con costos completos, no de todos los ciclos registrados. El grupo de 120 s no contiene ciclos elegibles para esta tabla.

| Span | Ciclos incluidos | Baseline total | Costo real total | Ahorro: baseline − real |
| ---: | ---: | ---: | ---: | ---: |
| 20 s | 124 | 85.326.567,59 CR | 85.326.567,59 CR | 0,00 CR |
| 60 s | 103 | 52.782.144,62 CR | 59.786.941,20 CR | −7.004.796,58 CR |
| 120 s | 0 | No aplicable | No aplicable | No aplicable |

La resta del grupo de 60 s se verifica: `52.782.144,62 − 59.786.941,20 = −7.004.796,58 CR`. Respecto de su propio baseline agregado, equivale a −13,27 %; es una **normalización descriptiva de ese grupo**, no una estimación del efecto causal del span. Para 20 s, la igualdad de las dos sumas produce ahorro cero en la métrica local; no demuestra que esperar 20 s sea mejor. La ausencia de compras tampoco prueba costo cero: el costo real puede incluir desbalance y otros componentes. Comparar directamente los totales de 124 y 103 ciclos sería engañoso, y la mezcla no cuantificada de baselines `SETTLEMENT` y `STATUS_ESTIMATE` limita incluso una comparación normalizada.

## Wallet: cronología, disponibilidad y latencias

La [instrumentación HTTP](../../master/app/services/wallet_observability.py) registra endpoint, resultado y `latencyMs`; la herramienta [`qa/e2067/latencias.py`](../../qa/e2067/latencias.py) puede calcular percentiles por ciclo si existe correlación explícita por `cycleId` o `idpk`. Las mediciones siguientes provienen, en cambio, de **una ventana concreta de logs del scheduler** entre las 04:30 y las 06:30 UTC del 1 de noviembre. No son latencias agregadas por span.

| Momento o tramo (UTC) | Evidencia observada | Interpretación y límite |
| --- | --- | --- |
| 1 nov., 02:45:33 | Última compra registrada, bajo 60 s. | Ocurrió 2 h 27 min 47 s antes del inicio aproximado de la interrupción sostenida. |
| 1 nov., antes de ~05:13:20, dentro de la ventana examinada | 47/47 `GET /balance` respondieron HTTP 200; p50 **465,03 ms**, p95 **543,53 ms**. | Latencias de solicitudes exitosas de balance, no de swaps ni de un span específico. |
| 1 nov., desde ~05:13:20, dentro de la ventana examinada | 129/129 solicitudes examinadas terminaron en errores de transporte; duración p50 **150,48 ms**, p95 **232,49 ms**. | Son tiempos hasta el fallo, **no latencias de operaciones Wallet exitosas**. |
| 2 nov. | Se ejecutó 120 s y comenzó el período documentado de 20 s. | Ambos grupos se observaron después del inicio de la incidencia; falta una atribución de solicitudes por ciclo. |
| 4 nov., ventana posterior de 30 minutos | 30/30 solicitudes examinadas recibieron HTTP 200; las métricas examinadas aún no mostraban compras nuevas. | Se comprobó recuperación de conectividad en esa ventana, no equivalencia de condiciones de mercado. |

La interrupción coincide temporalmente con gran parte de los grupos de 120 y 20 s y compromete su comparabilidad con 60 s. **No explica por sí sola toda la cronología:** la última compra precedió a la interrupción sostenida y no se observaron nuevas compras en las métricas examinadas tras la ventana de recuperación. El resumen de logs no permite estimar latencia de Wallet por span, duración total de la incidencia ni qué fracción de ciclos estuvo expuesta; esas variables requieren correlación por ciclo y tiempos de ejecución.

## Mercado compartido y decisiones de otras ciudades

Otra consulta productiva contabilizó **363 swaps** en el conjunto observado: 16 `SETTLED`, 341 `REJECTED`, cuatro `SUBMITTED` y dos `EXPIRED`. En los 341 rechazos examinados, los eventos oficiales `swap-rejected` indicaban `INSUFFICIENT_ASSET` para propuestas hacia MET: la contraparte no disponía de los activos necesarios para aceptar esas operaciones. Cuatro swaps históricos `SUBMITTED` carecían de `swapId` conocido y conservaban reservas; su estado es incierto y **no deben contarse como compras liquidadas**.

Estos 363 swaps **no están desglosados por span ni necesariamente coinciden con los 259 ciclos del experimento**. Por tanto, los 341 rechazos no se asignan a 20, 60 o 120 s ni se presentan como explicación única de la ausencia de compras. Sí ilustran un mecanismo externo plausible: la liquidez real, el precio y la capacidad de la contraparte dependen de decisiones de otras ciudades. Publicar o retirar ofertas, competir por una oferta y aceptar o rechazar bids también puede cambiar el resultado de un ciclo. El [plan E2-066](E2-066-plan-spans.md) propone observar ofertas, precios efectivos, rutas y ventanas para separar estas condiciones; el resumen productivo suministrado aquí no contiene esos covariables por ciclo.

## Hallazgos, amenazas a la validez y recomendaciones

1. **Observación:** hubo compras registradas solo en el grupo de 60 s. Nueve de sus ciclos compradores tenían costos finales y seis seguían esperando liquidación de desbalance. El ahorro acumulado de sus 103 ciclos `FINAL` fue negativo en la métrica local.
2. **Sesgo temporal y operativo:** 60 s se observó antes de la interrupción sostenida de Wallet; 120 s y 20 s, después de su inicio. El grupo de 120 s es pequeño y no tiene cierres `FINAL`. La recuperación posterior sin nuevas compras observadas impide atribuir todo el patrón a Wallet.
3. **Medición económica:** el corte no desglosa `baselineSource`, cobertura, déficit, rechazos ni costos por ciclo. Son campos consultables en `cycle_strategy_metrics`, no datos inexistentes. La comparación estricta debe distinguir baseline de settlement confirmado de estimaciones, excluir estados pendientes y revisar eventuales multas fuera de la fórmula local.
4. **Mercado no controlado:** las decisiones y restricciones de otras ciudades alteran oportunidades de compra. Las cifras de rechazos Wallet son globales al conjunto observado y no identifican qué span se vio afectado.

Para una réplica comparable conviene extraer filas por `cycle_id` con `strategy_version`, `span_seconds`, `deficit_kwh`, `covered_kwh`, `rejected_bids`, los componentes monetarios y `baseline_source`; vincularlas con capturas temporales del libro de ofertas y con telemetría Wallet atribuible por identificador. Se deben informar por separado los ciclos `AWAITING_SETTLEMENT` y los `FINAL`, y estratificar los períodos anterior, durante y posterior a la incidencia. Si se busca inferir el efecto del span, harán falta grupos más equilibrados y rotados bajo condiciones operativas comparables, como proponía el diseño original.

**Conclusión.** El experimento documenta actividad y costos reales del nodo, pero las diferencias entre grupos son observacionales y están confundidas por el tiempo, la disponibilidad de Wallet y el mercado compartido. Con este corte **no es posible demostrar que un span sea superior ni recomendar un valor óptimo**.
