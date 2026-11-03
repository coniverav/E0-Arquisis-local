# E2-089 — Anomalia 2: bid malformado e `IDENTITY_MISMATCH`

## Objetivo

Verificar que un bid malformado pero correlacionable devuelve `nack` con razon `MALFORMED_MESSAGE` (codigo 422), que un mensaje cuyo `cityId` contradice el `user_id` AMQP autenticado devuelve `nack` con `IDENTITY_MISMATCH` (codigo 403) y que los mensajes imposibles de correlacionar se **descartan** sin publicar una respuesta insegura. Ninguno debe ingresar en la logica de negocio, provocar un segundo efecto ni derribar al consumidor.

**Precisiones:** el `nack` de protocolo se publica antes del ACK del mensaje entrante cuando puede correlacionarse. El `reject(requeue=False)` del broker corresponde a los casos descartados, no es sinonimo del `nack` del protocolo.

## Casos de prueba

| Caso | Entrada de ejemplo | Salida esperada | Auditoria |
|---|---|---|---|
| Bid malformado | `cityId=COR`, `user_id=city.COR`, `data` sin `quantity` | Respuesta `nack` 422 a `city.COR`, `target=msgId`, ACK al broker | `NACKED`, `reasonCode=MALFORMED_MESSAGE` |
| Identidad incompatible | `cityId=COR`, `user_id=city.HGW` | Respuesta `nack` 403 a **city.HGW** (ruta autenticada), ACK al broker | `NACKED`, `reasonCode=IDENTITY_MISMATCH`, `sender=HGW` |
| JSON sin estructura / sin `msgId` | Payload no decodificable o no correlacionable | No generar `nack`; `reject(requeue=False)` | `DISCARDED` con metadatos disponibles |

La prueba de auditoria usa registros **sinteticos** y verifica que `/audit/anomalies` y `/audit/intercity` muestran los codigos como `reasonCode`, nunca como un estado `IDENTITY_MISMATCH`, y sin publicar el payload crudo.

## Evidencia del codigo

- `connector/tests/test_city_identity.py`: planificacion de respuesta, ruta autenticada, `NACKED` y `DISCARDED`, sin invocar negocio para casos invalidos.
- `connector/tests/test_directed_ack_nack.py`: `nack` correlacionado y direccionado, orden de publicacion vs. ACK del broker y descarte sin respuesta.
- `connector/tests/test_connector_audit.py`: metadata conservada por los constructores de auditoria.
- `master/tests/test_e2075_audit_v3.py`: consulta de auditoria, filtros por `reasonCode`, proteccion de payloads.
- `master/tests/test_e2088_e2089_audit_demo.py`: creacion local de auditoria de fallos y verificacion de la consulta sin nuevos `MarketBid`, `SwapRecord` ni `LedgerEntry`.

## Ejecucion

Desde el repositorio **local**:

```bash
./probar_E2_088_089.sh
```

Guardar `datos_e2088_089/<fecha>_connector.txt`, `datos_e2088_089/<fecha>_backend.txt` y el commit de la rama como evidencia. El script construye una imagen local de pruebas y levanta una base PostgreSQL efimera, sin publicar al broker ni realizar HTTP a Wallet Central. El conector se prueba en una red Docker `none`; el backend en una red Docker interna con su unica base de prueba.

## Inspeccion en ambiente desplegado (solo lectura)

```http
GET /audit/anomalies?status=NACKED&reasonCode=MALFORMED_MESSAGE
GET /audit/anomalies?status=NACKED&reasonCode=IDENTITY_MISMATCH
GET /audit/anomalies?status=DISCARDED
GET /audit/intercity?reasonCode=IDENTITY_MISMATCH
Authorization: Bearer <JWT_DE_OPERADOR>
```

Si el filtro devuelve cero resultados, significa que no se encontro un evento con esas condiciones en la ventana de datos consultada. No crear mensajes malformados en el broker productivo para forzar evidencia.

## Resultado y limites

**Estado de esta ficha:** casos preparados y pruebas existentes identificadas; adjuntar la salida real antes de declarar aprobada la tarjeta. No se ha comprobado una publicacion real de NACK frente a la central ni la configuracion del gateway/roles productivo. Depende de E2-015, E2-016 y E2-075.
