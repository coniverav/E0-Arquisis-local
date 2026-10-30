# E2-075 — Consulta de auditoría v3 (RF05)

Esta API consulta evidencia persistida en Master. Las respuestas son de solo lectura y no recalculan conciliaciones. Los endpoints nuevos requieren `Authorization: Bearer <token>` con JWT válido para el issuer y audience configurados y un `sub` no vacío. El control de roles más fino depende de la política del gateway/Auth0 del despliegue; debe verificarse allí antes de exponerlos públicamente.

## Consultas

| Ruta | Filtros | Contenido |
| --- | --- | --- |
| `GET /audit/anomalies` | `status`, `type`, `reasonCode`, `msgId`, `idpk`, `sender`, `cycleId`, `offset`, `limit` | Vista reducida ya usada por Frontend: `DUPLICATE`, `DISCARDED`, `NACKED`; `IDENTITY_MISMATCH` figura como `reasonCode`. Conserva `{total, items}` y ahora acepta `sender`, `cycleId` y `offset`. |
| `GET /audit/intercity` | `cycleId`, `idpk`, `msgId`, `cityId`, `status`, `reasonCode`, `direction`, `offset`, `limit` | Inbound/outbound de `bid`, `bid-accepted`, `bid-rejected`, `ack`, `nack`. Incluye `direction`, `msgId`, `idpk`, `type`, `cycleId`, `cityId`, `status`, `reasonCode`, `targetMsgId`, `occurredAt`. `msgId` también encuentra respuestas cuyo `targetMsgId` apunta al mensaje buscado. |
| `GET /audit/wallet-events` | `cycleId`, `idpk`, `swapId`, `status`, `offset`, `limit` | Eventos `swap-*` recibidos, con vínculo a `swapRecordId`, `bidIdpk`, `ledgerEntryIds`, `statementEntryIds`, `balanceSnapshotIds` y última conciliación del ciclo. |
| `GET /audit/reconciliations` | `cycleId`, `status`, `offset`, `limit` | Resultados durables de AD5, deltas CR/KWH y operaciones comparadas por tipo, activo, montos, estado e IDs de asientos. `MATCH`, `MISMATCH`, `PENDING_*` y `WALLET_ONLY` corresponden a operaciones; el estado de conciliación conserva los valores internos existentes. |

Las tres rutas nuevas devuelven `{total, offset, limit, items}`. `limit` por defecto es 50 y admite hasta 100. `offset` comienza en 0. Ordenan de más reciente a más antiguo. Las consultas usan los datos que Master ya procesó; una publicación todavía en outbox puede mostrar `PENDING`.

Ejemplos de consumo desde Frontend tras obtener el token habitual:

```http
GET /audit/intercity?cycleId=<CYCLE_ID>&cityId=<CITY_ID>&limit=50
Authorization: Bearer <JWT_DE_USUARIO>

GET /audit/wallet-events?swapId=<SWAP_ID>&limit=50
Authorization: Bearer <JWT_DE_USUARIO>

GET /audit/reconciliations?cycleId=<CYCLE_ID>&limit=50
Authorization: Bearer <JWT_DE_USUARIO>
```

No se devuelve `data.token`, `swapToken`, `Idempotency-Key`, API key ni payload crudo en las nuevas rutas. El serializador interno de inbound devuelve sólo los identificadores auditados del envelope y `rawPayload: null`, incluso para mensajes malformados; el original sigue guardado en PostgreSQL para diagnóstico. Los NACK salientes exponen `reasonCode` únicamente si la razón persistida corresponde a un código conocido del protocolo. El acceso a PostgreSQL y a `/internal/*` debe protegerse mediante la red privada/gateway del despliegue. `GET /audit/anomalies` conserva su acceso actual para no romper el Frontend y solo entrega metadatos reducidos.

## Validación tras despliegue

1. Consultar `/audit/anomalies?status=DUPLICATE` y `/audit/anomalies?reasonCode=IDENTITY_MISMATCH`; comprobar que `IDENTITY_MISMATCH` nunca aparece como estado.
2. Buscar un `msgId` de un bid en `/audit/intercity` y verificar tanto la recepción como el ACK/NACK o la decisión correlacionada.
3. Buscar un `swapId` de prueba en `/audit/wallet-events` y contrastar sus IDs de ledger, statement y conciliación con los logs de Master.
4. Consultar `/audit/reconciliations?cycleId=...` y comparar montos/estados con el balance y statement Wallet de ese ciclo, sin publicar tokens en logs de demo.
5. Verificar 401 sin token, 403 sin identidad y los controles de acceso del gateway/Auth0 para las nuevas rutas. No se ha validado aquí la configuración productiva.
