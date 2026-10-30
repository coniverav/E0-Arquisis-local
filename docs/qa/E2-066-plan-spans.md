# Plan de ensayo E2-066 — RF06

Se evaluarán `spanSeconds` = **0, 30, 60, 120 y 240**. La diferencia estudia cuánto esperar antes de emitir compras. `span=0` es control de compra inmediata, `60` es el valor inicial AD6, `30` representa una espera corta y `120`/`240` aumentan la exposición al riesgo de cierre y cambios de liquidez.

## Hipótesis

**H0 (descriptiva):** no se observan diferencias consistentes en cobertura, costo normalizado o bids rechazados entre valores de span.

**H1 (exploratoria):** esperar puede permitir conocer mejores ofertas y disminuir costo, pero también perder liquidez o expirar la ventana de bids. No se presupone un signo único del efecto.

## Comparación

1. **Misma configuración:** conservar `offerSelectionPolicy`, `bidAcceptancePolicy`, markup y fracción; modificar únicamente `spanSeconds` a través de E2-062 antes del próximo ciclo.
2. **Bloques:** cinco bloques de cinco ciclos, con rotación del orden de spans; todos aparecen una vez en cada posición. Duración aproximada con ciclos de 20 min: 8h20, sin contar pausas.
3. **Resultado confirmado:** para cada `cycleId`, leer E2-065. Comparar `rejectedBids`, `coveredKwh`, `coverageRate`, `realCost`, `baselineCost`, `savingsVsBaseline`, `strategyVersion`, `spanSeconds`. Solo comparar económicamente `FINAL` y baseline de settlement confirmado.
4. **Normalización:** `savingsRate = (baselineCost - realCost) / baselineCost`, `realCostPerDeficitKwh = realCost / deficitKwh`, guardando costos originales para auditoría.
5. **Mercados variables:** medir cantidad de ofertas, liquidez relativa, precio efectivo/imbalance, rutas deshabilitadas, aparición tardía y ventana `lastBidAt`. Estratificar/excluir datos sesgados, sin presentar diferencias observacionales como causalidad.
6. **Escenarios sintéticos controlados:** ejecutar seis fixtures con oferta y ruta fijas para los cinco spans. Etiquetar siempre como simulación idealizada, no como producción.

## Criterios de exclusión y transparencia

Rechazar comparaciones en ciclos sin métrica E2-065, `status != FINAL`, baseline estimado en vez de confirmado, estrategia/span distintos del plan, déficit cero y `span >= lastBidAt - opensAt`. Reportar número de observaciones y exclusiones por span, sin imputar ceros.

## Limitaciones

Los ciclos reales ocurren secuencialmente y son distintos; los bloques reducen sesgos de orden, pero **no aseguran condiciones de mercado iguales**. Para concluir efectividad se requiere contrastar los resultados con covariables de mercado, suficientes repeticiones y revisión manual del equipo. Ningún script interviene el broker en modo de prueba.
