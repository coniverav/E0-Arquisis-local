# Continuidad de E1/E2 para la Entrega 3

## Propósito y alcance

Esta revisión establece la línea base que debe mantenerse durante la migración a E3. No es un documento puntuado por sí solo, pero responde directamente a los gates G01 y G02: la aplicación debe continuar operativa en la nube y conservar las dos colas, el protocolo v3, los ciclos autónomos, los reportes, HTTPS, autenticación, monitoreo y alertas de presupuesto.

Se revisaron los tres repositorios y se hicieron dos comprobaciones públicas de solo lectura el **17 de noviembre de 2026**. No se accedió a AWS, RabbitMQ, Wallet, Auth0 ni New Relic con credenciales, por lo que la presencia de código o configuración no se presenta como prueba de operación productiva.

## Evidencia pública actual

| Comprobación | Resultado | Qué demuestra | Qué no demuestra |
| --- | --- | --- | --- |
| `GET https://api.energyshark-g10.tech/health` | HTTP 200, TLS válido; `{"status":"ok","instance":"master"}` | La ruta pública llega a una instancia FastAPI que puede ejecutar `SELECT 1` en PostgreSQL. | No verifica la segunda réplica, scheduler, connector, broker, Wallet ni reportes. |
| `GET https://app.energyshark-g10.tech/` | HTTP 200, TLS válido, `text/html` | El frontend público se distribuye por HTTPS. | No valida login, token, rutas protegidas ni que el build corresponda al último commit. |

## Matriz de continuidad

Los estados utilizados son: **verificado públicamente**, **respaldado en código/pruebas**, **evidencia histórica** y **pendiente de verificación productiva**.

| Flujo que debe mantenerse | Evidencia concreta disponible | Estado actual | Riesgo E3 y acción requerida |
| --- | --- | --- | --- |
| Aplicación completa en cloud y PostgreSQL persistente | [`docker-compose.prod.yml`](../../docker-compose.prod.yml) define PostgreSQL con volumen, migración, dos instancias API, scheduler y connector. `/health` respondió 200 y comprueba DB. | Verificado públicamente sólo para `master` y DB. | **Alto:** una migración de sagas/outbox puede dañar la continuidad. Probar Alembic sobre copia E2, respaldar antes del corte y verificar todos los contenedores. |
| Consumo de `city.KLD.q` y `market.KLD` | [`consume_forever`](../../connector/connector.py) y [`consume_market_forever`](../../connector/connector.py); pruebas en `connector/tests/test_market_consumer.py` y `test_connector_flow.py`. | Respaldado en código/pruebas; no revalidado hoy contra broker. | **Crítico:** los consumidores EC2 deben permanecer; Lambda no puede consumir las colas del curso. Confirmar logs/heartbeat y entrega real de ambas colas antes y después del despliegue. |
| Identidad AMQP y ACK/NACK dirigido | `authenticated_inbound_route`, `plan_protocol_response` y pruebas `test_city_identity.py`, `test_directed_ack_nack.py`. Se acepta `user_id="central"` en mensajes dirigidos y `market-closed` conserva el tratamiento del feed. | Respaldado en código/pruebas. | **Alto:** los callbacks de sagas no deben saltarse validación ni producir respuestas recursivas. Ejecutar regresión del Connector y contratos v3.0.2. |
| Ciclos autónomos de 20 minutos | [`cycle_scheduler.py`](../../master/app/workers/cycle_scheduler.py) procesa ventanas, ofertas, bids, swaps, cierres y conciliación. `status-statement`, `trade-recorded` e `imbalance-settlement` tienen pruebas específicas. | Respaldado en código/pruebas. | **Crítico:** Step Functions reemplaza coordinación de swaps, no la autoridad temporal de la Central ni el scheduler completo. Hacer un ciclo de regresión antes del primer swap E3. |
| `negotiation-report` oportuno | `enqueue_due_negotiation_reports` se ejecuta desde el scheduler; `test_negotiation_report_dispatch.py` cubre ventana, reintento y estados. | Respaldado en código/pruebas; no se verificó un reporte productivo reciente. | **Crítico:** perder un reporte cuesta 50.000 CR. Migrarlo al outbox sin cambiar su ventana, `idpk`, correcciones ni tratamiento de errores. |
| Wallet REST, eventos y ledger local | [`wallet_client.py`](../../master/app/services/wallet_client.py), `wallet_service.py`, `cycle_accounting.py`, `trade_events.py` y `wallet_reconciliation.py`; suites de recuperación, eventos y conciliación. | Respaldado en código/pruebas; Wallet productiva no consultada. | **Crítico:** la saga debe conservar `Idempotency-Key`, cuotas, `Retry-After` y autoridad de Wallet. El consumidor aplica ledger antes de reanudar el callback. |
| Frontend y backend separados por HTTPS | Dominios públicos respondieron 200. README documenta CloudFront/S3 y API Gateway/EC2; los repositorios siguen separados. | Verificado públicamente en disponibilidad básica; topología AWS no inspeccionada. | **Alto:** SSE puede requerir una ruta distinta por límites de API Gateway. Conservar HTTPS/CORS y justificar la ruta autenticada elegida. |
| Autenticación funcional | Backend valida JWT mediante JWKS, issuer y audience en [`auth.py`](../../master/app/auth.py). Frontend usa `Auth0Provider` y agrega Bearer token desde `src/services/api.js`. | Respaldado en código; login y authorizer productivos no probados hoy. | **Crítico:** `EventSource` no envía Bearer arbitrario de forma nativa. Definir autenticación SSE sin exponer tokens ni abrir el feed. |
| New Relic APM e infraestructura | Compose ejecuta `master`, `master2`, scheduler y connector con `newrelic-admin` y contiene `newrelic-infra`. [`E2-061-alertas-panel-wallet-broker.md`](../observability/E2-061-alertas-panel-wallet-broker.md) documenta eventos y consultas. | Configurado en repositorio; existe evidencia histórica, no verificación actual de ingestión o alertas. | **Alto:** integrar métricas de Lambda/Step Functions vía CloudWatch, conservar correlación `cycleId`/`swapId`/`idpk` y comprobar notificaciones de las cinco alertas. |
| AWS Budget Alerts | [`gates-checklist.md`](gates-checklist.md) registra el budget E2 como verificado históricamente. No existe definición versionada del budget ni acceso actual a Billing. | Evidencia histórica; pendiente de verificación productiva. | **Bloqueante para G02:** ampliar cobertura a Step Functions y Lambda, comprobar umbral y destinatarios y conservar evidencia actual. |
| Contratos v3.0.2 y CI | Contracts conserva 46 schemas y 261 fixtures del tag oficial v3.0.2, commit `62f6cf40768b6e42717d126e528dcd944e5bc0e3`; su workflow valida schemas y flujos reales del backend. | Respaldado en repositorio/CI; estado actual de GitHub Actions no consultado. | **Alto:** E3 no cambia el protocolo. Mantener contract tests verdes al reemplazar coordinación interna. |
| Persistencia y deduplicación | PostgreSQL contiene claims por `idpk`, auditoría, outbox E2, reservas, swaps y hechos Wallet; existen pruebas de duplicados y eventos fuera de orden. | Respaldado en código/pruebas. | **Crítico:** el nombre de ejecución complementa, pero no sustituye, la deduplicación local. Migraciones E3 deben ser aditivas y preservar datos E2. |

## Riesgos prioritarios

1. **Pérdida de ciclos o reportes durante el corte.** El mercado continúa con 72 ciclos diarios; la migración debe ser aditiva y tener rollback de contenedores sin revertir o recrear PostgreSQL.
2. **Doble coordinación de swaps.** Mientras se habilitan las sagas, el scheduler E2 y Step Functions no pueden ejecutar simultáneamente el mismo camino. El corte debe seleccionar una única autoridad de coordinación por operación.
3. **Callback antes de persistir el token.** Un evento de Wallet puede llegar antes que el proceso local guarde cómo reanudar la saga. El token y sus correlaciones deben persistirse antes del POST.
4. **Outbox E2 insuficiente para el gate nuevo.** La publicación existente ayuda, pero E3 exige atomicidad explícita también para `StartExecution`, publisher confirms y recuperación medible.
5. **SSE y autenticación.** API Gateway y `EventSource` pueden condicionar streaming, timeout y transporte del token. Debe resolverse antes de comprometer una ruta productiva.
6. **Evidencia histórica confundida con estado actual.** New Relic, Auth0, broker, Wallet y budgets requieren una nueva comprobación productiva. La documentación previa no demuestra que estén operativos el día de la entrega.
7. **Infraestructura manual no reproducible.** EC2, API Gateway, CloudFront y observabilidad están documentados, pero todavía deben declararse en IaC sin importar ni destruir los recursos actuales.

## Criterio de salida de la auditoría

La línea base queda identificada, pero **G01/G02 todavía no pueden declararse cerrados para E3**. Antes del primer despliegue se debe registrar: estado de todos los contenedores, mensajes reales en ambas colas, último reporte oportuno, login y ruta protegida, consulta Wallet de solo lectura, entidades/host visibles en New Relic y budget actualizado para Lambda/Step Functions.
