# E2-081 y E2-082 — Revisión de ADR y post-mortems

**Corte documental:** 3 de noviembre de 2026. **Base:** archivos del ZIP del repositorio E2 facilitado para esta revisión. Se inspeccionaron implementación, pruebas y evidencia ya archivada. No se ejecutó la suite completa ni se consultó wallet central durante esta revisión.

## Alcance y criterio de evidencia

Se mantuvieron intactas las decisiones originales de ADR4–ADR6. Cada ADR incorpora al final una **revisión retrospectiva E2-081** con mecanismo observado, prueba existente, tradeoff y diferencia frente al diseño. Se actualizaron los tres documentos de `docs/adr/postmortems` para E2-082, retirando afirmaciones desactualizadas como «E2 todavía no implementada» cuando ya existe código.

Se distinguen tres niveles: **implementación inspeccionada**, **prueba local con resultado almacenado** y **comportamiento de producción observado**. La presencia de un archivo `test_*.py` no se presenta como una prueba ejecutada. Una wallet simulada no sustituye a la wallet central. El ZIP no incluye el historial `.git`, por lo que **no permite verificar en qué commits se introdujeron los ADR en relación con el código**.

## E2-081 — Contraste entre decisiones y resultados

| ADR | Resultado observado | Evidencia más fuerte disponible | Diferencia / límite |
|---|---|---|---|
| **AD4 — Serialización** | Se utiliza bloqueo `FOR UPDATE` por oferta, reserva `HOLD` e intención de swap durable; el HTTP se realiza fuera del lock. | `datos_e2069/RESUMEN_E2_069.md`: 13 escenarios locales `PASS`, incluyendo 80+80 sobre 100 KWH con separación nominal 40 ms. | No hay prueba con dos compradores reales y wallet central, ni medición oficial de `INSUFFICIENT_ASSET`. |
| **AD5 — Consistencia** | Ledger con efectos idempotentes diferenciados y conciliación `/balance` + `/statement`, correlación por IDs y estados de discrepancia. | `test_e2068_wallet_recovery.py`, `test_e2070_reconciliation.py` y servicios asociados. | Pruebas presentes pero sin resultado de ejecución adjunto en el ZIP; no hay conciliación oficial de cierre archivada. |
| **AD6 — Configuración** | Estrategias `PENDING` versionadas, fijadas al ciclo; API de edición y auditoría, métricas RF06. | `test_strategy_config_ad6.py`, `test_e2062_strategy_edit.py` y `test_e2065_cycle_metrics.py`. | No hay frontend en este ZIP ni capturas de ciclos reales en `datos_e2066/` o `datos_e2067/`; RF06 sigue sin evidencia experimental de mercado real. |

## E2-082 — E1 frente al diseño efectivo de E2

| Decisión E1 | Elemento conservado | Adaptación E2 | Divergencia que debe revisarse |
|---|---|---|---|
| **AD1 — Broker** | Connector separado de API; persistencia y auditoría de recepción/publicación. | Dos consumidores: `city` dirigido con respuestas de protocolo y `market` sin ACK de protocolo; marca de brecha tras reconexión. | TTL/capacidad y pérdidas silenciosas del broker no son recuperables con un replay inexistente. |
| **AD2 — Ledger** | Historial de movimientos y snapshot por ciclo. | Wallet como autoridad; swaps, transporte, imbalance y conciliación de asientos oficiales. | La proyección puede diferir temporalmente; inmutabilidad estricta no impuesta por DB. |
| **AD3 — Timeouts** | Worker, reintentos persistidos e idempotencia de efectos. | Estados y vencimientos de swaps/bids, `Idempotency-Key`, GET de recuperación y cuota compartida. | El scheduler todavía invoca timeouts/reintentos de negociaciones E1; no confundirlos con `negotiation-report` vigente. |

## Evidencia reproducible y pendientes

**AD4:** el script [`probar_E2_069.sh`](../../probar_E2_069.sh) requiere una base de pruebas PostgreSQL aislada. El registro [`datos_e2069/RESUMEN_E2_069.md`](../../datos_e2069/RESUMEN_E2_069.md) incluye los 13 escenarios y JSON individuales. En el caso 80+80 se midieron offsets de inicio **1,68 ms** y **41,48 ms**; la separación observada fue cercana a 40 ms. Es una medición local, no una latencia de procesamiento comprometida.

**AD5:** ejecutar en entorno aislado los tests de recuperación y conciliación, adjuntar resultado con commit/fecha y contrastar posteriormente con `GET /balance` y el `/statement` oficial al cierre de un ciclo, sin usar `202` como liquidación.

**AD6:** ejecutar los tests de versiones y auditoría. Verificar edición desde una interfaz real sin reinicio, documentar ciclo anterior/siguiente con `strategyVersion` y `spanSeconds`, y ejecutar el plan [`E2-066`](../qa/E2-066-plan-spans.md) con capturas según [`E2-067`](../qa/E2-067-ejecucion-registro.md). Evitar presentar comparaciones simuladas como resultados causales de producción.

**AD1:** ensayar corte y recuperación de broker con ambas colas y observar backlog/TTL con métricas oficiales. **AD3:** comprobar si siguen activos los caminos de negociación E1 en el runtime v3 y validar recuperación de POST ambiguo con estado persistido. Estos pendientes no invalidan la implementación inspeccionada, pero limitan las afirmaciones de cumplimiento.

**Secuencia RDOC01:** ejecutar en el checkout Git real, para cada ADR:

```bash
git log --follow --format='%h %aI %s' -- docs/adr/ADR-004-serializacion-aceptacion-bids.md
git log --follow --format='%h %aI %s' -- docs/adr/ADR-005-consistencia-ledger-wallet.md
git log --follow --format='%h %aI %s' -- docs/adr/ADR-006-estrategia-configuracion.md
```

Comparar el primer commit de cada ADR con los commits que introdujeron sus servicios. **No declarar comprobado el orden de commits usando solamente este ZIP**. Las secciones retrospectivas deben quedar en un commit posterior con referencia a E2-081; los post-mortems actualizados, en un commit identificado como E2-082.

## Documentos actualizados

- [AD4](ADR-004-serializacion-aceptacion-bids.md), [AD5](ADR-005-consistencia-ledger-wallet.md) y [AD6](ADR-006-estrategia-configuracion.md): revisiones retrospectivas E2-081.
- [Post-mortem AD1](postmortems/postmortem-AD1-topologia-broker.md), [AD2](postmortems/postmortem-AD2-ledger.md) y [AD3](postmortems/postmortem-AD3-timeouts-idempotencia.md): actualización E2-082.
