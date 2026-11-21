# Post-mortem AD6: Estrategia versionada y experimento de span

### Decisión y resultado de E2

AD6 eligió estrategias versionadas en PostgreSQL, activadas al comienzo del siguiente ciclo. Un cambio de precio, cantidad, política o `span` debía quedar pendiente durante el ciclo abierto, sin redeploy y sin mezclar políticas dentro de un mismo resultado.

[`strategy_config.py`](../../../master/app/services/strategy_config.py) guarda la versión pendiente y la asocia al ciclo al recibir el `status-statement`. [`market_api.py`](../../../master/app/routers/market_api.py) expone edición, consulta y auditoría; la edición usa `expectedVersion` para evitar escrituras simultáneas. Las métricas guardan versión de estrategia y `span` efectivo junto con los resultados del ciclo.

### Evidencia disponible

- [`test_strategy_config_ad6.py`](../../../master/tests/test_strategy_config_ad6.py) cubre activación en el siguiente ciclo, inmutabilidad de la versión ya aplicada y llegada tardía de `status-statement`.
- [`test_e2062_strategy_edit.py`](../../../master/tests/test_e2062_strategy_edit.py) comprueba edición concurrente, `409` por versión obsoleta, auditoría y conservación de la estrategia del ciclo abierto.
- [`test_e2065_cycle_metrics.py`](../../../master/tests/test_e2065_cycle_metrics.py) cubre persistencia de `span`, versión, cobertura, baseline y costo real cuando existe liquidación.
- **Experimento del 4 de noviembre:** se aplicaron 20, 60 y 120 segundos a dos ciclos por configuración. Los spans 20 s (249881–249882) y 120 s (249885–249886) no tuvieron déficit. Con 60 s, el ciclo 249884 tuvo déficit de 4.216 KWH y no registró compra; los seis ciclos estaban `AWAITING_SETTLEMENT` al revisarlos. La configuración fue aplicada sin redeploy, pero no hubo costo real final ni baseline cerrado comparable.
- **Revisión del 5 de noviembre:** el ciclo 249932 tuvo déficit de 2.736 KWH y una oferta visible de COR, sin compra registrada. Wallet informó `cr_available = -74.457.539,53 CR` y `credit_line = 310.804,12 CR`, dejando capacidad neta de compra negativa. Los movimientos históricos contenían cargos de desbalance importantes.

El detalle y las limitaciones del experimento están en [`informe-experimento-span.md`](../../qa/informe-experimento-span.md). No hay latencias de Wallet correlacionadas con los seis ciclos, ni una cantidad de bids rechazados que permita comparar los grupos.

### Tradeoff y divergencias

**La decisión de fijar una estrategia por ciclo se mantiene.** Permitió aplicar y luego identificar los spans sin reiniciar los servicios, y evitó cambiar una política mientras su ciclo estaba en curso. A cambio, una edición no tiene efecto inmediato y el análisis depende de que el ciclo alcance liquidación y cuente con condiciones económicas comparables.

La principal divergencia entre el escenario deseado y los resultados observados fue la falta de transacciones útiles para comparar. Los spans se ejecutaron en momentos distintos de un mercado compartido: ofertas, precios, decisiones de otras ciudades y saldo de Wallet no fueron controlados. La capacidad de compra negativa observada después limita todavía más la posibilidad de atribuir una diferencia al tiempo de espera. También hubo degradación del publicador RabbitMQ durante el período, corregida posteriormente, pero no hay evidencia para asignarle causalidad sobre las liquidaciones pendientes o las compras ausentes.

### Cierre y acciones pendientes

AD6 queda **conservada para E3**. La estrategia seguirá decidiendo qué oferta elegir, cuánto ofrecer y cuándo intentar comprar; la saga sólo ejecutará esa decisión. Cada ciclo debe conservar versión, `span`, ofertas candidatas, déficit, cobertura, bids rechazados, baseline, costo real y resultado de Wallet.

Antes de comparar spans se requieren ciclos finalizados, capacidad de compra disponible y liquidaciones comprobables. Las conclusiones deberán seguir diferenciando los resultados de KLD de decisiones de otras ciudades y de la disponibilidad de Wallet; con la evidencia actual no corresponde declarar superior a 20, 60 o 120 segundos.
