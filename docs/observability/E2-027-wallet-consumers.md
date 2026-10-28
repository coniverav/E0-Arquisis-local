# E2-027 · Observabilidad wallet y consumidores

Esta implementación registra eventos seguros de **HTTP wallet** y de los consumidores
**city** y **market**. No altera confirmaciones AMQP ni la autoridad de wallet.

## Eventos de monitoreo

- `EnergySharkWalletHttp`: `method`, `endpoint` (ruta categorizada), `outcome`,
  `latencyMs`, `statusCode` si existe, `retryAfterSeconds` para 429 y `idpk`
  si es UUID. Un `202` significa solo petición aceptada, **no swap liquidado**.
- `EnergySharkBrokerDelivery`: `consumer=city|market`, `outcome=broker_ack|broker_reject`,
  `latencyMs`, `requeue` y metadatos seguros `msgId`, `idpk`, `type`, `cycleId`.
  Un `broker_ack` no equivale a ACK de protocolo ni a swap-settled.
- `EnergySharkBrokerConnection`: `consumer`, `state=connecting|connected|retrying`,
  `errorType` (solo clase de excepción). Consumidores con procesos de reconexión independientes.

Los datos también aparecen como logs estructurados `wallet_http`, `broker_delivery`
y `broker_connection` para diagnóstico local. El SDK New Relic publica los eventos
personalizados cuando el agente está habilitado. El backend ya corre con New Relic;
se agrega el agente al conector en `docker-compose.prod.yml`.

## Consultas NRQL sugeridas (New Relic)

```sql
SELECT count(*) FROM EnergySharkWalletHttp FACET endpoint, outcome SINCE 1 hour ago
```

```sql
SELECT percentile(latencyMs, 50, 95, 99) FROM EnergySharkWalletHttp FACET endpoint SINCE 1 hour ago
```

```sql
SELECT count(*) FROM EnergySharkBrokerDelivery FACET consumer, outcome SINCE 1 hour ago
```

```sql
SELECT count(*) FROM EnergySharkBrokerConnection FACET consumer, state SINCE 1 hour ago
```

## Límites y privacidad

No se registra `X-Api-Key`, encabezados, cuerpos HTTP, cuerpos AMQP, tokens,
ni URLs completas. El error HTTP no incluye el cuerpo de respuesta. Se correlacionan
operaciones por el UUID `idpk` y mensajes por `msgId` sin registrar secretos.
La validación de JWT/token pertenece al comprador (RF03), la serialización de bids
a AD4, la conciliación wallet-ledger a AD5 y la estrategia efectiva a AD6.

## Ejecución de pruebas

```bash
# En imagen master (sin llamadas externas)
pytest -q tests/test_wallet_observability.py

# En imagen connector (sin broker real)
pytest -q tests/test_observability.py
```

**Nota:** la publicación de eventos en New Relic se verifica en producción
con el agente y su licencia reales; las pruebas unitarias comprueban instrumentación
local y privacidad, no ingestión del servicio SaaS.
