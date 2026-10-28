# E2-056 — Historial E2 de ciclos (RF01)

`GET /cycles/{cycle_id}/history` devuelve el historial persistido del ciclo, sin modificar el ledger ni consultar la wallet. Devuelve `404` si el ciclo no existe. No altera `GET /cycles/{cycle_id}` (E1).

La respuesta presenta `statusStatement`, ventanas del mercado, `funding` (`TRANSFER_IN`, incluyendo `details.funding` cuando existe), `offers`, `offerRevisions` (mensajes de publicación/actualización/retiro), `bids`, `swaps`, `trades` (incluye `transportTotal`, `routeEnabled`), `settlements`, `negotiationReports`, `ledger` y `lastOperation`. Cada lista se ordena por su marca temporal y por ID, y el ledger por secuencia.

- `offerRevisions.publicationStatus` es el estado de publicación de RabbitMQ, **no** aceptación comercial.
- `swaps.proposalHttpAcceptedAt` solo registra la recepción HTTP 202. El resultado se consulta en `swaps.status`; el settlement económico lo indica el evento wallet correspondiente.
- Un `swapToken` y las claves de idempotencia de la wallet **no** se publican en esta respuesta.
- `lastOperation` se obtiene del último efecto persistido en el ledger por número de secuencia, no del último mensaje de RabbitMQ. Si no hay efectos, es `null`.
- Los arreglos sin registros permanecen vacíos. No se fabrican eventos faltantes ni saldos finales.

Este endpoint es intencionalmente solo lectura para las vistas frontend de RF01. **No** crea mensajes, cambia estados de bids/swaps ni llama a la wallet.

Pruebas: `master/tests/test_e2056_cycle_history.py` (404, ciclo sin eventos, historial completo y protección de token, aislamiento entre ciclos).
