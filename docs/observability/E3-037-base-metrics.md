# E3-037 — Contrato de eventos y métricas base (RNF01)

**Estado:** propuesta lista para revisión. **Periodo interno:** 23–24/11/2026. **Alcance:** definición; la activación y verificación de alertas corresponden a E3-063…E3-070.

## Objetivo

Definir las **cinco señales** pedidas en RNF01 con una fuente verificable, cálculo, criterio de alerta, tiempo objetivo de detección, canal y campos de correlación. Este documento describe **objetivos propuestos**, no umbrales ya medidos ni alertas configuradas.

## 1. Matriz de señales

| ID | Señal, unidad y cálculo | Fuente comprobada en el proyecto | Condición y umbral **propuestos** | Detección objetivo y destino |
|---|---|---|---|---|
| `wallet_latency_p95_ms` | Percentil 95 de `latencyMs` por endpoint y ventana de 5 min; contar `http_429`, `timeout` y `transport_error` en paralelo | `master/app/services/wallet_observability.py` publica `EnergySharkWalletHttp` con `endpoint`, `latencyMs`, `outcome`, `statusCode`, `idpk` | p95 mayor que `2 × p95_E2` durante 5 min. **Pendiente** recuperar p95 real E2 y fijar umbral numérico en ms | ≤ 5 min; correo del equipo (dirección pendiente) |
| `consumer_signal_age_seconds` y `consumer_delivery_age_seconds` | Segundos desde último heartbeat de cada consumidor y, por separado, desde último ACK procesado | `connector/observability.py`: `EnergySharkBrokerHeartbeat`, `EnergySharkBrokerConnection`, `EnergySharkBrokerDelivery` con `consumer=city/market` | alerta si conexión `disconnected` o pérdida de heartbeat durante 120 s; también advertencia de **0 mensajes procesados durante un ciclo de 20 min** cuando se esperen entregas. Evitar confundir silencio normal con caída | ≤ 3 min para desconexión; ≤ 21 min para falta de mensajes durante un ciclo; correo |
| `ledger_delta_cr` y `ledger_delta_kwh` | Discrepancia local vs Wallet al cierre, por activo. Usar `cr_unexplained`, `kwh_unexplained` y `status`, sin convertir `NULL` en cero | `wallet_reconciliations`, alimentada por `master/app/services/wallet_reconciliation.py`; el estado `DIVERGENT` indica discrepancia | `status = DIVERGENT` al cierre, o `abs(delta) > 0` **cuando conciliación esté completa**. `NO_LOCAL_BASELINE` y `EXPLAINED` son estados separados, no equivalen automáticamente a discrepancia | ≤ 2 min desde conciliación de cierre; correo |
| `sagas_open_after_close_count` y `saga_failures_per_cycle` | Contar ejecuciones no terminales > `market_closes_at + 60 s`, por ciclo; contar `FAILED` y `TIMED_OUT` y dividir por ejecuciones iniciadas | Tablas `saga_executions`, `cycles`; AWS Step Functions `ExecutionFailed` / `ExecutionTimedOut` via CloudWatch para corroboración | **Umbral exigido AD7:** 0 ejecuciones abiertas 60 s después del cierre. Propuesta adicional: ≥ 1 saga fallida/timed out por ciclo genera aviso y revisión; ajustar ruido con datos reales | ≤ 60 s tras la tolerancia AD7; correo |
| `outbox_oldest_pending_age_seconds` | `now - MIN(created_at)` para filas con `dispatch_required = true` y estado `PENDING`, `CLAIMED` o `FAILED`; 0 cuando no hay pendientes | `outbound_messages`, `created_at`, `dispatch_required`, `status` en `master/app/models.py` | Umbral preliminar de **30 s** para señal temprana. **AD8 exige** volver a atraso 0 en ≤ 60 s desde recuperación; medir `recovered_at` y `drained_at` para verificarlo | ≤ 1 min; correo |

**Importante:** los valores de 120 s, 30 s y las ventanas de detección anteriores son **propuestas operativas del equipo**. El enunciado solo fija numéricamente los plazos AD7 (60 s) y AD8 (60 s), y exige que el resto de umbrales se justifiquen con telemetría real.

## 2. Contrato mínimo de eventos

Campos comunes sugeridos: `eventType`, `timestamp` UTC, `cityId`, `cycleId`, `swapId`, `idpk`, `sagaExecutionArn`, `component`, `status`, `durationMs`, `reasonCode` cuando aplique. Son identificadores de correlación, no métricas de alta cardinalidad para agrupar series.

- **WalletHttp:** `endpoint`, `latencyMs`, `outcome`, `statusCode`, `retryAfterSeconds`. Ya existe `EnergySharkWalletHttp` en backend; comprobar además la visibilidad de las llamadas hechas desde Lambda en CloudWatch/New Relic, porque una Lambda no usa automáticamente el wrapper del backend.
- **BrokerHeartbeat / BrokerDelivery / BrokerConnection:** `consumer` (`city`, `market`), `state` o `outcome`, `latencyMs`, `sourceAgeSeconds` cuando exista. Ya hay emisores en `connector/observability.py`.
- **LedgerReconciled:** `cycleId`, `status`, `crUnexplained`, `kwhUnexplained`, `walletAsOf`, `lastSettledCycle`. **Propuesto** emitir evento desde resultado durable de `wallet_reconciliations` sin publicar datos sensibles.
- **SagaTransition / SagaTerminal:** `cycleId`, `swapId`, `idpk`, `executionArn`, `role`, `state`, `status`, `transitionAt`. **Propuesto** emitir/ingestar eventos de la ejecución, y cotejar la tabla local con CloudWatch.
- **OutboxState:** `rowId`, `cycleId`, `swapId`, `idpk`, `deliveryKind`, `status`, `attemptCount`, `oldestPendingAgeSeconds`. **Propuesto** como medición de gauge independiente; no publicar envelopes ni task tokens.

Las trazas relacionadas con swaps deben incluir `cycleId`, `swapId` e `idpk` cuando existan y permitir localizar su Execution History por ARN. Nunca incluir X-Api-Key, secretos, JWT completos ni `taskToken` en logs.

## 3. Consultas verificables en PostgreSQL

### 3.1 Última conciliación Ledger–Wallet

```sql
SELECT cycle_id, status, cr_unexplained, kwh_unexplained, created_at
FROM wallet_reconciliations
ORDER BY created_at DESC, id DESC
LIMIT 1;
```

### 3.2 Sagas abiertas más de 60 s después del cierre

```sql
SELECT s.cycle_id, COUNT(*) AS open_sagas, ARRAY_AGG(s.idpk) AS idpks
FROM saga_executions s
JOIN cycles c ON c.cycle_id = s.cycle_id
WHERE c.market_closes_at IS NOT NULL
  AND NOW() > c.market_closes_at + INTERVAL '60 seconds'
  AND s.status NOT IN ('SETTLED', 'REJECTED', 'COMPENSATED', 'TIMED_OUT', 'FAILED')
GROUP BY s.cycle_id;
```

**Nota:** corroborar adicionalmente en CloudWatch ejecuciones AWS abiertas que aún no se hayan reflejado en el estado local. `market_closes_at` representa `closesAt` del protocolo v3.

### 3.3 Edad de la fila outbox pendiente más antigua

```sql
SELECT COALESCE(
    EXTRACT(EPOCH FROM NOW() - MIN(created_at)), 0
) AS oldest_pending_age_seconds,
COUNT(*) AS pending_count
FROM outbound_messages
WHERE dispatch_required = true
  AND status IN ('PENDING', 'CLAIMED', 'FAILED');
```

Para AD8, además registrar cuándo se restableció el broker/proceso (`recovered_at`) y cuándo `pending_count` llegó a 0 (`drained_at`). La comprobación es `drained_at - recovered_at <= 60 segundos`, no solo edad de la fila.

## 4. Consultas iniciales para New Relic (adaptar al nombre de cuenta)

Latencia de Wallet por endpoint, usando la telemetría que ya se emite:

```sql
SELECT percentile(latencyMs, 95) FROM EnergySharkWalletHttp FACET endpoint SINCE 30 minutes ago
```

Tasa de errores Wallet:

```sql
SELECT count(*) FROM EnergySharkWalletHttp WHERE outcome IN ('timeout', 'transport_error', 'http_429') SINCE 30 minutes ago TIMESERIES 5 minutes
```

Últimos heartbeats por consumidor:

```sql
SELECT latest(timestamp) FROM EnergySharkBrokerHeartbeat FACET consumer SINCE 30 minutes ago
```

La consulta NRQL por sí sola no crea ninguna alerta. E3-063…067 deberán configurar condiciones, objetivos de detección y destinatario, realizar disparos controlados y conservar capturas de la notificación recibida.

## 5. Criterios de aceptación de E3-037

- [ ] Las cinco señales están descritas con unidad, cálculo, origen y correlación.
- [ ] El equipo acuerda umbrales finales. Wallet p95 se fija a partir de datos E2 y no a partir de un valor inventado.
- [ ] `city` y `market` se evalúan por separado; heartbeat no equivale a mensaje procesado.
- [ ] Una conciliación sin baseline no se interpreta como diferencia cero.
- [ ] El umbral AD7 de 60 s tras `closesAt` queda explícito.
- [ ] El objetivo AD8 de recuperar el atraso del outbox en ≤ 60 s queda explícito.
- [ ] Se documentan campos y ruta para buscar `cycleId`, `swapId`, `idpk` y Execution History.
- [ ] Se acuerda canal de aviso y responsable de E3-063…E3-070.

## 6. Dependencias y seguimiento

Depende de E3-010, E3-015 y E3-025. Alimenta E3-063 (Wallet), E3-064 (consumer), E3-065 (ledger), E3-066 (sagas), E3-067 (outbox), y posteriormente E3-069/070 (pruebas de alertas y runbook).

**Evidencia de E3-037:** este contrato versionado en `/docs/observability/`, referencia al código que emite cada señal, y aprobación de umbrales propuestos por el equipo. No declarar RNF01 completado hasta configurar y probar las cinco alertas en el SaaS.
