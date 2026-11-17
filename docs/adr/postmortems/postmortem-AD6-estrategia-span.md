# Post-mortem AD6: Estrategia versionada y experimento de span

**Revisión retrospectiva:** 17 de noviembre de 2026. Este post-mortem revisa [AD6](../ADR-006-estrategia-configuracion.md) usando el experimento realizado por KLD durante E2. La intención no es elegir retrospectivamente una estrategia ganadora, sino establecer qué se demostró y qué no permiten concluir los datos.

## Escenario prometido

AD6 propuso que la política comercial fuera una configuración versionada en PostgreSQL, editable sin redeploy y activada al inicio del ciclo siguiente. Así, un ciclo abierto conserva la versión que le corresponde y es posible atribuir a esa versión el `span`, la cobertura del déficit, los bids rechazados y los costos del mercado.

El escenario era editar, por ejemplo, el `span` a mitad de un ciclo. La edición debía quedar pendiente, no alterar el ciclo ya abierto y aplicarse de forma consistente al siguiente, aun con más de una instancia del backend.

## Qué ocurrió en E2

La versión efectiva de la estrategia quedó fijada por ciclo. La edición crea una versión pendiente y el siguiente `status-statement` la asocia al ciclo nuevo; la interfaz muestra qué configuración está activa y cuál está pendiente. Las pruebas cubren edición concurrente, conflicto de versión, llegada tardía de `status-statement`, conservación de la estrategia del ciclo abierto y registro de métricas por versión.

El experimento del 4 de noviembre aplicó 20, 60 y 120 segundos a dos ciclos por configuración. Las seis configuraciones entraron en vigor, lo que sí confirma el aspecto operativo central de AD6: fue posible cambiar el `span` sin redeploy y luego identificar el valor usado por cada grupo de ciclos.

| Span | Ciclos | Déficit observado | Compras | Estado al revisar |
| ---: | --- | ---: | ---: | --- |
| 20 s | 249881–249882 | 0 KWH | 0 KWH | `AWAITING_SETTLEMENT` |
| 60 s | 249883–249884 | 4.216 KWH, sólo en 249884 | 0 KWH | `AWAITING_SETTLEMENT` |
| 120 s | 249885–249886 | 0 KWH | 0 KWH | `AWAITING_SETTLEMENT` |

Hubo un único déficit dentro de los seis ciclos y su cobertura fue 0 de 4.216 KWH. Los otros cinco no permiten medir el efecto de esperar para comprar porque no requirieron energía. Como ninguno de los seis contaba entonces con liquidación final de desbalance, no se obtuvo costo real final ni baseline cerrado comparable. Tampoco se registró una cantidad de bids rechazados suficiente para una comparación entre grupos.

Una revisión posterior del 5 de noviembre encontró el ciclo 249932 con déficit de 2.736 KWH y una oferta visible de COR, también sin compras. En ese momento Wallet reportó `cr_available = -74.457.539,53 CR` y `credit_line = 310.804,12 CR`; la capacidad neta de compra era negativa. Los movimientos históricos mostraban cargos importantes por desbalance. Ese contexto explica por qué no basta con observar una oferta para esperar que ocurra una compra y por qué no es posible usar esos ciclos como una comparación limpia de spans.

## Factores que limitaron la interpretación

El experimento fue observacional: los spans se aplicaron en momentos distintos y las ofertas, precios, saldos y decisiones de otras ciudades no fueron controlados. Wallet no entregó latencias correlacionadas con los seis ciclos, por lo que no hay una comparación de tiempos de respuesta entre spans. Durante el período hubo degradación del publicador RabbitMQ, corregida posteriormente, pero su coincidencia temporal no prueba que fuera la causa de las liquidaciones pendientes ni de la falta de compras.

Por estas razones, los datos no permiten declarar ganador a 20, 60 o 120 segundos, ni atribuir causalmente al span la ausencia de compras o el costo económico. Sí muestran que una configuración versionada permite registrar de forma honesta cuándo una comparación carece de condiciones suficientes.

## Balance y cambios propuestos para E3

La decisión de fijar una estrategia por ciclo se mantiene. Evitó mezclar una edición nueva con decisiones de un ciclo ya abierto y dejó una base para observar el resultado con sus parámetros reales. Su costo es que la edición no tiene efecto inmediato y que el análisis necesita esperar cierres y liquidaciones definitivas.

En E3, AD6 seguirá fuera de la saga: la estrategia decide qué oferta elegir, cuánto ofrecer y cuándo intentar comprar; la saga ejecuta de forma durable esa decisión. Los datos por ciclo deberán incluir versión, `span`, ofertas candidatas, déficit, cobertura, bids rechazados, costos de baseline y costo real, junto con el resultado oficial de Wallet. Antes de comparar spans se debe confirmar capacidad de compra positiva y ciclos finalizados. Las conclusiones seguirán separando resultados de KLD de factores externos, en especial las decisiones de otras ciudades y la disponibilidad de Wallet.

## Evidencia consultada

- [`Informe del experimento de span`](../../qa/informe-experimento-span.md).
- [`test_strategy_config_ad6.py`](../../../master/tests/test_strategy_config_ad6.py).
- [`test_e2062_strategy_edit.py`](../../../master/tests/test_e2062_strategy_edit.py).
- [`test_e2065_cycle_metrics.py`](../../../master/tests/test_e2065_cycle_metrics.py).
