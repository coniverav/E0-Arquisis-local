# E2-038 — Envío de bid dirigido

- `POST /internal/market/bids` con `cycleId`, `offerId`, `quantity` e `idpk` UUID.
- Revalida la ventana (`opensAt + span`, `lastBidAt`) y la selección AD6/E2-037 en la misma transacción protegida con el lock de ciudad.
- Sólo acepta cantidades enteras, `minBid <= quantity <= remaining`, sin superar las solicitudes locales aún pendientes/aceptadas.
- Guarda juntos `MarketBid` y el sobre completo en `OutboundMessage`, `dispatch_required=True`, `exchange=energy.x`, `routing_key=city.{seller}` y vencimiento en `lastBidAt`.
- El conector existente toma la outbox y publica con `user_id=city.{COMPRADOR}`. `PUBLISHED` significa **publicado en RabbitMQ**, no `ACCEPTED`.
- Repetir la llamada HTTP con el mismo `idpk` y los mismos datos no crea otra compra. Si el envío falla, el dispatch crea otro `msgId` con el **mismo `idpk`**. No envía mensajes vencidos.
- El cuerpo v3 contiene `offerId` y `quantity`, **no incluye price**.
- E2-018 exige una FK `MarketBid.offer_id -> MarketOffer.offer_id`: se mantiene una referencia descriptiva para satisfacerla. La fuente vigente de precios/remaining sigue siendo `MarketFeedOffer` y la wallet conserva autoridad sobre saldos AD5.
- Este endpoint es **interno**: debe permanecer restringido a la red interna o protegido por autenticación de servicio en el despliegue. No ejecuta un scheduler/autocomprador, que deberá invocar la función de encolado en su propio proceso.
