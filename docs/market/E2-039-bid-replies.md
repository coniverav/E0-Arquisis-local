# E2-039 — Correlacionar bid-accepted y bid-rejected

- Ambos llegan a la cola `city.{comprador}` por el conector existente, que valida el `user_id` AMQP, persiste recepción en el backend y genera **ACK de protocolo independiente**.
- Procesamiento de `bid-accepted` y `bid-rejected` en `/internal/messages`, con claim transaccional de `idpk` y auditoría.
- Correlación de `data.target` con **cualquier intento** de la outbox de `bid`, incluso reenvíos con nuevo `msgId` y el mismo `idpk`. Comprueba ciclo, vendedor y `offerId` contra el bid original.
- En `bid-accepted`, exige quantity íntegra, precio y amount consistente (`round2(quantity × pricePerEnergy)`), `swapId`, `swapToken`, `expiresAt` válidos. Marca el bid `ACCEPTED` y guarda `swapId`; **NO** descuenta saldos, acepta el swap ni marca `SETTLED`. Validación criptográfica del swapToken y POST al wallet pertenecen al flujo de liquidación.
- En `bid-rejected`, valida `reason/code` oficial y registra `available` solamente cuando `reason=QUANTITY_UNAVAILABLE`. Marca `REJECTED`; una nueva solicitud por el remanente requerirá **otro `idpk`** y un nuevo bid.
- Si una respuesta llega duplicada, se conserva el estado terminal sin repetir efectos. Si el idpk es el mismo, `ProcessedIdpk` previene procesamiento múltiple; si hay respuesta terminal conflictiva se audita `NOT_PROCESSED`.
- El ACK o resultado de publicación de RabbitMQ **no** se interpreta como aceptación comercial.
