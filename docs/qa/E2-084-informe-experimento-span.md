# Informe del experimento de span

## Objetivo y metodología

Se compararon ciclos de producción agrupados por el tiempo de espera antes de intentar comprar energía (`span`). Se observaron los ciclos registrados, los que llegaron a estado `FINAL`, las compras y el ahorro frente al costo estimado de no participar en el mercado.

## Valores probados y ciclos

| Span | Ciclos registrados | Ciclos `FINAL` | Ciclos con compras | Energía comprada | Ahorro acumulado comunicado |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 20 s | 134 | 124 | 0 | 0 kWh | No informado |
| 60 s | 119 | 103 | 15 | 109.624 kWh | −7.004.796,58 CR |
| 120 s | 6 | 0 | No informado | No informado | No calculable con ciclos `FINAL` |

Se registraron 259 ciclos en total y 227 llegaron a `FINAL`. No se proporcionaron identificadores individuales, fechas de cada grupo ni el detalle de las políticas aplicadas en cada ciclo. Los seis ciclos de 120 s no permiten comparar costos finales.

## Observaciones económicas y de cobertura

Las compras comunicadas se concentraron en 15 ciclos del grupo de 60 s. No se informó el déficit de esos ciclos ni su cobertura efectiva; los kWh comprados no equivalen necesariamente a kWh de déficit cubierto. Tampoco se entregaron las sumas separadas de costo real y costo estimado sin mercado (*baseline*) para ninguno de los spans.

El ahorro acumulado comunicado para 60 s fue negativo. Si se calculó como `baseline − costo real` sobre los mismos ciclos, significa que el costo real superó al baseline en 7.004.796,58 CR. Sin el desglose por ciclo y los criterios usados para incluirlos, no es posible verificar ese agregado ni atribuirlo al span. La ausencia de compras en 20 s tampoco implica costo real cero: puede haber costos de desbalance o liquidación.

## Mediciones y disponibilidad de Wallet

Durante parte del experimento se reportaron problemas de disponibilidad de Wallet. No se entregaron mediciones de latencia, duración de la incidencia, tasas de error, resultados de conciliación ni eventos de liquidación desglosados por span. Por ello este informe **no asigna tiempos de respuesta ni fallos de Wallet a un grupo concreto** y no presenta las compras como verificadas independientemente contra su estado final en Wallet.

## Hallazgos, limitaciones y conclusión

En estos resultados hubo compras reportadas con 60 s, ninguna con 20 s y ningún ciclo `FINAL` con 120 s. Esto describe lo observado, pero **no demuestra que 60 s sea superior** ni que el span por sí solo explique las diferencias. Los grupos tienen tamaños muy distintos y fueron ejecutados bajo condiciones diferentes; la disponibilidad de Wallet también pudo influir.

Otros grupos experimentaban simultáneamente. Sus decisiones sobre cuándo publicar o retirar ofertas, qué precios fijar y qué bids aceptar podían cambiar la oferta disponible para nuestra ciudad. No hay registros por ciclo que permitan medir ese efecto o separar su influencia de la del span. Sin esos datos, la cobertura por ciclo, el desglose de costos y las mediciones de Wallet, no puede hacerse una comparación causal ni elegir un span óptimo a partir de este experimento.
