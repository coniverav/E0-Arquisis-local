# Informe del experimento de span

## Objetivo y método

El span es el tiempo que espera KLD antes de intentar comprar energía en el mercado. Queríamos observar si esperar 20, 60 o 120 segundos cambiaba la cobertura del déficit y el costo de un ciclo. El 4 de noviembre de 2026 aplicamos cada valor a dos ciclos consecutivos y comprobamos que las tres configuraciones entraron en vigor. La comparación es observacional, los ciclos ocurrieron en momentos distintos de un mercado compartido. Las cifras provienen del corte de ciclos y Wallet compartido para esta revisión; no se hizo una nueva extracción de producción.

## Resultados del 4 de noviembre

| Span | Ciclos | Déficit registrado | Compras | Estado al revisar |
| ---: | --- | ---: | ---: | --- |
| 20 s | 249881–249882 | 0 kWh | 0 kWh | Ambos `AWAITING_SETTLEMENT` |
| 60 s | 249883–249884 | 4.216 kWh, solo en 249884 | 0 kWh | Ambos `AWAITING_SETTLEMENT` |
| 120 s | 249885–249886 | 0 kWh | 0 kWh | Ambos `AWAITING_SETTLEMENT` |

De los seis ciclos, solo **249884** necesitó energía. Como no compró, la cobertura de ese déficit fue **0 de 4.216 kWh**. Los otros cinco ciclos no permiten medir cobertura de un déficit que no existió. Tampoco hay una diferencia observable en compras entre los tres spans, ninguno registró una.

Los seis ciclos seguían pendientes de liquidación de desbalance. Por eso **no disponemos de costos reales de cierre ni de un baseline final comparable** para calcular ahorro por span. La evidencia reunida tampoco cuantifica bids rechazados en estos ciclos.

## Lo que mostró la revisión posterior

El 5 de noviembre se comprobaron ciclos recientes que sí habían finalizado, con liquidaciones contablemente consistentes. En el ciclo **249932** hubo un déficit de **2.736 kWh** y se observó una oferta de COR, pero tampoco se registró una compra. En esa revisión, Wallet informó un saldo disponible de **−74.457.539,53 CR** frente a una línea de crédito de **310.804,12 CR**. La capacidad de compra seguía siendo negativa. Los movimientos históricos mostraron cargos importantes por desequilibrio energético, que ayudan a explicar el deterioro de la posición económica de KLD.

La revisión incluyó esos saldos informados por Wallet y la consistencia contable de liquidaciones recientes. No hay latencias de Wallet correlacionadas con los seis ciclos del ensayo, por lo que no corresponde comparar tiempos de respuesta entre spans. Durante el ensayo también se observó una degradación del publicador RabbitMQ, corregida después. Su coincidencia temporal no demuestra que haya causado la falta de liquidaciones de estos seis ciclos.

## Interpretación y conclusión

Este ensayo confirmó que pudimos aplicar los tres tiempos de espera, pero no permite elegir una configuración ganadora, hubo un solo ciclo deficitario, ninguna compra y ningún costo final comparable. El mercado depende además de las ofertas, precios y decisiones de otras ciudades, que cambian entre ciclos. La oferta observada en 249932 no convierte a los grupos del 4 de noviembre en condiciones equivalentes. La capacidad de compra negativa observada después limita aún más la posibilidad de comparar los spans mediante transacciones reales.

Una comparación económica útil requeriría más ciclos con déficit, capacidad de compra disponible y liquidaciones finales. En cada uno habría que registrar ofertas observadas, energía cubierta, bids rechazados, costo efectivo, baseline y resultado de Wallet, manteniendo separados los ciclos pendientes de los finalizados. Hasta contar con esa evidencia, las diferencias atribuibles al span siguen sin poder medirse.
