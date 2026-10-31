# E2-061 · Alertas y panel mínimo de errores de wallet y broker

**Dependencias:** E2-027 (eventos de observabilidad) y E2-048 (sistema desplegado).
**Criterio:** distinguir timeout, HTTP 429 / Retry-After, reconexión y mensajes consumidos desde `market.{CODE}`.
**Requisito asociado:** RNF05, G08 y demo de anomalías (situación 7).

## Qué se monitorea

| Situación | Evento New Relic | Dato para distinguirla | Qué muestra el panel |
| --- | --- | --- | --- |
| Timeout de wallet | `EnergySharkWalletHttp` | `outcome = 'timeout'` | errores HTTP por categoría y percentiles de latencia |
| Rate limit y espera | `EnergySharkWalletHttp` | `statusCode = 429`, `retryAfterSeconds` | cantidad de 429 por endpoint y duración del `Retry-After` recibido |
| Reconexión broker | `EnergySharkBrokerConnection` | `state = 'retrying'`, `consumer` | reconexiones separadas para `city` y `market` |
| Cola market realmente consumida | `EnergySharkBrokerDelivery` | `consumer = 'market'`, `outcome` | ACK o REJECT **del broker** después del procesamiento |
| Consumidor market conectado sin mensajes | `EnergySharkBrokerHeartbeat` | `consumer = 'market'` | latido cada 60 s mientras la conexión está activa |

En E2-027 ya se capturan las solicitudes de wallet y las entregas de ambos consumidores. E2-061 agrega un latido de salud específico para `market`, porque **cero mensajes recibidos no significa que la cola haya fallado**; puede no haber ofertas durante un intervalo.

`broker_ack` **no** es un ACK del protocolo de aplicación. Un HTTP `202` de wallet **no** es un swap liquidado. Esta tarjeta registra comportamientos técnicos sin cambiar la lógica de wallet, la idempotencia, el ledger ni la confirmación de mensajes.

## Cambios en ejecución

- `connector/observability.py`: evento seguro `EnergySharkBrokerHeartbeat` (sin contenido de mensajes).
- `connector/connector.py`: un latido inmediato y luego cada 60 s mientras la conexión `market` está abierta; se cancela si se desconecta y se reactiva cuando reconecta.
- `docker-compose.prod.yml`: el `cycle-scheduler` ahora ejecuta `newrelic-admin run-program` y tiene el agente configurado. Es importante porque los `POST` y `GET` a wallet se realizan tanto desde los servicios HTTP como desde el scheduler.
- `tools/newrelic_e2061.py`: definición reproducible de dashboard y alertas vía NerdGraph, sin requerir paquetes Python nuevos.

## Dashboard

El dashboard `EnergyShark E2 | Wallet y broker` contiene 8 gráficos: errores wallet, HTTP 429, latencias p50/p95, `Retry-After`, reconexiones `city/market`, entregas consumidas de `market`, latidos del consumidor y reencolados de `market`. El selector temporal permite observar el incidente de la demo.

### Alertas (umbrales iniciales de demo)

1. **Timeout wallet:** al menos un timeout en una ventana de agregación de 60 s.
2. **Wallet 429:** al menos una respuesta `429` en 60 s.
3. **Reconexión broker:** al menos un evento `retrying` en 60 s.
4. **Market reencolado:** al menos un rechazo `broker_reject` con `requeue = true` en 60 s.
5. **Heartbeat market perdido:** abrir alerta si no llega `EnergySharkBrokerHeartbeat` durante 300 s, una vez observada la primera señal.

Las alertas de eventos esporádicos usan `EVENT_TIMER`; se cierran por pérdida de señal después de 300 s sin nuevas anomalías, mientras la del heartbeat abre una anomalía al perder la señal. Los umbrales son intencionalmente sensibles para las pruebas y pueden producir avisos durante reconexiones breves: revisar después del ensayo.

**La creación de políticas/condiciones no configura automáticamente un destinatario.** Debe vincularse la política a un workflow de notificaciones en New Relic si se desean correos o mensajes. Sin esa configuración, las condiciones pueden generar issues visibles en New Relic, pero no entregar notificaciones externas.

## Publicar en la cuenta del grupo

No subir ninguna API key al repositorio. Los valores de ejemplo no sirven para desplegar.

```bash
# Primero comprobar la definición sin conectarse a New Relic
python tools/newrelic_e2061.py --account-id NUMERO_DE_CUENTA

# Solo en el entorno privado del operador (no en el repositorio)
export NEW_RELIC_ACCOUNT_ID=NUMERO_DE_CUENTA
export NEW_RELIC_USER_API_KEY='USER_KEY_DE_NEW_RELIC'
python tools/newrelic_e2061.py --apply

# En cambios posteriores, utilizar el GUID impreso para actualizar el mismo panel
export NEW_RELIC_DASHBOARD_GUID='GUID_DEL_PANEL_CREADO'
python tools/newrelic_e2061.py --apply
```

- La key requerida por NerdGraph es la **user API key**, distinta de la `NEW_RELIC_LICENSE_KEY` utilizada por los agentes.
- El script reutiliza por nombre la política de alertas y las condiciones existentes. **No actualiza automáticamente condiciones ya creadas**. Cambios posteriores de umbrales deben hacerse en la UI de New Relic o recreando la condición de forma controlada.
- Sin `NEW_RELIC_DASHBOARD_GUID`, cada ejecución con `--apply` crea un dashboard adicional. Guardar el GUID en una ubicación privada.
- El dashboard se crea `PUBLIC_READ_ONLY` para poder mostrarlo sin permitir que otros cambien sus widgets.
- Revisar en producción `NEW_RELIC_MONITOR_MODE=true` tanto para `master`, `master2`, `cycle-scheduler` como para `connector`. Verificar también que la clave de ingesta corresponda a la cuenta correcta.
- Configurar el workflow de notificaciones para la política `EnergyShark E2 | Anomalias wallet y broker`, si el grupo desea alertas fuera de la UI.

## Registro de aplicaciones de procesos en segundo plano

El `connector` y `cycle-scheduler` se ejecutan continuamente sin atender peticiones web.
Ambos deben llamar a `newrelic.agent.register_application(timeout=0.0)` al comenzar
si `NEW_RELIC_MONITOR_MODE` está habilitado. La inicialización del agente con
`newrelic-admin run-program` por sí sola no demuestra que sus eventos personalizados
se estén enviando. El registro es asíncrono para no bloquear el consumo ni el worker.

Comprobar en New Relic tanto `EnergySharkBrokerHeartbeat` como `EnergySharkWalletHttp`
tras desplegar, sin utilizar `Python Agent Test` como prueba de la aplicación real.

## Validación de evidencia para la demo

1. **Wallet con timeout:** provocar una llamada simulada en ambiente de prueba, buscar `EnergySharkWalletHttp` con `outcome='timeout'` y verificar el gráfico y la condición. No generar operaciones duplicadas en el mercado real.
2. **HTTP 429:** comprobar `statusCode=429` y `retryAfterSeconds` cuando la wallet devuelva efectivamente ese header; comprobar que el cliente respetó la espera en las pruebas de integración. No inundar el servicio para provocar el rate limit.
3. **Desconexión breve del broker:** observar `retrying` y luego `connected` con el mismo `consumer`. Comprobar reconexión automática, sin intervención manual.
4. **Consumo de market:** observar `broker_ack` o `broker_reject` en `EnergySharkBrokerDelivery` con `consumer='market'`, junto al heartbeat. Un feed silencioso con heartbeat activo indica conexión sin entregas recientes; no debe presentarse como consumo exitoso.
5. **Alerta real:** revisar issues en New Relic y el canal de notificaciones vinculado, dejando capturas del panel antes, durante y después de la anomalía.

Para consultar directamente:

```sql
SELECT count(*) FROM EnergySharkWalletHttp FACET outcome SINCE 1 hour ago
SELECT max(retryAfterSeconds) FROM EnergySharkWalletHttp WHERE statusCode = 429 SINCE 1 hour ago
SELECT count(*) FROM EnergySharkBrokerConnection FACET consumer, state SINCE 1 hour ago
SELECT count(*) FROM EnergySharkBrokerDelivery WHERE consumer = 'market' FACET outcome SINCE 1 hour ago
SELECT count(*) FROM EnergySharkBrokerHeartbeat WHERE consumer = 'market' TIMESERIES SINCE 1 hour ago
```

## Pruebas locales

```bash
python -m unittest discover -s tools/tests -v
cd connector
python -m pytest -q tests/test_observability_e2027.py tests/test_observability_e2061.py
```

Los tests validan forma de las consultas, cobertura de casos, política sin duplicados, ejecución en dry-run y emisión segura de heartbeat. **No prueban ingesta efectiva en New Relic, permisos NerdGraph, ni envío de alertas**, porque necesitan una cuenta real y un despliegue operativo. El script sólo crea recursos en New Relic con `--apply` explícito.

Referencias oficiales usadas para parametrizar NerdGraph:
- https://docs.newrelic.com/docs/apis/nerdgraph/examples/nerdgraph-dashboards/
- https://docs.newrelic.com/docs/apis/nerdgraph/examples/nerdgraph-api-alerts-policies/
- https://docs.newrelic.com/docs/apis/nerdgraph/examples/nerdgraph-api-nrql-condition-alerts/
- https://docs.newrelic.com/docs/alerts/create-alert/fine-tune/choose-your-aggregation-method/
