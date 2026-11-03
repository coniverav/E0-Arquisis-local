# E2-088 — Anomalia 1: replay `swap-settled` y `bid` con mismo `idpk`

## Objetivo y alcance

Demostrar que una reentrega con el **mismo `idpk` y distinto `msgId`** no crea un segundo bid, reserva, `SwapRecord`, POST a Wallet ni asiento `SWAP_SETTLED`. El duplicado debe quedar visible en auditoria y correlacionado con el `msgId` original.

**Entorno:** QA local, PostgreSQL desechable, broker AMQP en memoria y Wallet `httpx.MockTransport`. No se publica en RabbitMQ oficial, ni se efectuan pagos reales.

## Evidencia del codigo vigente

- `master/tests/test_e2046_dedup_replay.py::test_seller_bid_and_wallet_replays_do_not_repeat_economic_effects`: construye un ciclo vendedor, registra bid, envia replay de bid con `msgId` nuevo, procesa `swap-proposed` y `swap-settled`, vuelve a enviar `swap-settled` con el mismo `idpk`, luego con otro `idpk` pero igual `swapId`, y reenvia el bid.
- Comprueba un `MarketBid`, una `OfferReservation`, un `SwapRecord`, un POST Wallet, una instantanea EVENT y un asiento `SWAP_SETTLED` para ese swap. Confirma `PROCESSED` + `DUPLICATE` para los `idpk` repetidos. La cuenta total del ledger contiene un asiento de liquidacion adicional respecto de los asientos previos del ciclo; no implica que el ciclo solo tenga un asiento.
- `master/tests/test_e2088_e2089_audit_demo.py::test_e2088_replay_mismo_idpk_auditado_y_correlacionado`: usa dos filas **sinteticas** con `idpk` compartido y `msgId` diferentes, comprueba `/audit/anomalies` (incluye `relatedMsgId` original) y `/audit/wallet-events` (ambas recepciones, metadatos sin payload). Esta segunda prueba valida la proyeccion de auditoria, no repite la liquidacion.
- Implementacion: `master/app/routers/internal_messages.py` reclama el `idpk` con `claim_idpk`, marca `DUPLICATE` cuando el contenido economico coincide y `NOT_PROCESSED` con `IDPK_PAYLOAD_CONFLICT` si difiere. Para hechos de Wallet, el efecto economico se protege tambien con claves por `swapId` en las tablas de dominio.

## Ejecucion y evidencias

Desde la **copia local**, nunca en `/opt/energyshark` ni en EC2:

```bash
./probar_E2_088_089.sh
```

El script genera `datos_e2088_089/<fecha>_backend.txt`. Guardar ese archivo y el `git rev-parse HEAD` de la rama ejecutada como anexos de la tarjeta. Un PASS es evidencia de **simulacion local**, no de replay observado en broker productivo.

## Verificacion adicional de demo, solo lectura

Con JWT de operador autorizado, buscar en la auditoria un `idpk` de prueba realmente procesado, si existe de forma natural:

```http
GET /audit/anomalies?status=DUPLICATE&type=bid&idpk=<IDPK>
GET /audit/anomalies?status=DUPLICATE&type=swap-settled&idpk=<IDPK>
GET /audit/wallet-events?swapId=<SWAP_ID>
Authorization: Bearer <JWT_DE_OPERADOR>
```

Verificar `msgId`, `idpk`, `relatedMsgId`, `status` y `swapId`; contrastar el numero de asientos `SWAP_SETTLED` y swaps con una consulta de solo lectura de un entorno de prueba. El `relatedMsgId` aparece en `/audit/anomalies`, no en `/audit/wallet-events`.

**No ejecutar una reentrega con identificadores reales en produccion** ni registrar tokens de swap o credenciales en los anexos.

## Resultado y limites

**Estado de esta ficha:** caso y comandos preparados, resultados de ejecucion por adjuntar. Las aserciones de la suite expresan las invariantes esperadas, no un PASS realizado por esta documentacion. Depende de E2-046, E2-042 y E2-075.
