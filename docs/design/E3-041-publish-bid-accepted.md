# E3-041 — Publicar bid-accepted tras swap-proposed

## Decisión y fuente de verdad

La recepción autenticada de `swap-proposed` por el connector y el endpoint
`master/app/routers/internal_messages.py` es la única ruta autorizada para
publicar una aceptación comercial. Dentro de **la misma transacción**,
`WalletService.process_swap_event` persiste el estado Wallet y
`enqueue_bid_accepted_after_swap_proposed` valida el evento, JWT y reserva,
crea `OutboundMessage(bid-accepted)` y actualiza `MarketBid(ACCEPTED)`.
El relay broker publica lo ya confirmado en PostgreSQL.

La tarea Step Functions `PublishBidAccepted` **no crea otro mensaje**. La Lambda
`publish-bid-accepted` comprueba que la transacción Wallet dejó una única
respuesta durable con `swapId`, `swapToken`, destino, vencimiento, economía y
correlaciones coincidentes con la saga. Esto evita dos productores del mismo
bid y conserva los identificadores del outbox E2. La respuesta Lambda no revela
el JWT ni la clave de Wallet.

## Condiciones

1. La saga existe, corresponde al bid vendedor y conserva su `idpk`.
2. El bid ya está `ACCEPTED`, el SwapRecord está `PROPOSED` o `SETTLED`, la
   reserva sigue `HOLD` o pasó a `COMMITTED` y existe el token persistido.
3. El mensaje `bid-accepted` está en outbox BROKER, es único, coincide con
   el `msgId` del bid original, los valores congelados y la ciudad compradora.
4. El token/expiración debe seguir vigente. `PENDING`, `CLAIMED` y `FAILED`
   indican **encolado**, mientras `PUBLISHED` acredita publicación. La Lambda
   no llama RabbitMQ directamente ni reenvía un mensaje ya publicado.
5. Sin evidencia de evento/outbox, falla en modo cerrado con
   `SellerBidAcceptedPending`; Step Functions permite reintentos breves ante
   carreras de commit. Otras inconsistencias desvían a reconciliación.

## Límites pendientes

- El paso de E3-040 desde el evento Wallet hacia la reanudación del task token
  aún requiere validación end-to-end en AWS/connector (E3-024 y siguientes).
- `202 Accepted` en POST /swaps nunca constituye `swap-proposed`.
- No activar `E3_SELLER_SAGA_ENABLED` en producción hasta completar las tareas
  seller restantes, reconciliación, terminales y compensaciones.

## Pruebas

- `python -m unittest discover -s infra/cdk/tests -p 'test_e3041*.py' -v`
- `cd master && python -m pytest -v tests/test_e3041_seller_bid_accepted_db.py`
- Complementar con `tests/test_seller_bid_accepted.py` (contrato E2 v3) y
  `infra/cdk/tests/test_saga_asl.py` (ASL).
