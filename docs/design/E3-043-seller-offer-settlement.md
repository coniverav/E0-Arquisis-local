# E3-043 — Settlement seller, remaining y offer-updated

## Alcance

La recepción autenticada de `swap-settled` sigue siendo responsabilidad de
`master/app/routers/internal_messages.py` y `WalletService.process_swap_event`.
No se introduce una segunda ruta Lambda que aplique ledger, modifique ofertas o
publique directamente en RabbitMQ.

La confirmación `COMMITTED` de `OfferReservation`, el decremento de
`MarketOffer.remaining_quantity`, el asiento `SWAP_SETTLED` y el outbox
`offer-updated` deben confirmarse en la **misma transacción** del ingreso Wallet.
Si el evento falla o se revierte, ninguno de esos efectos persiste.

## Refuerzo E3-043

- La selección de `SwapRecord` al procesar eventos Wallet usa `FOR UPDATE`,
  tanto si la correlación llega por `swapId`, `idpk`, clave de aceptación o
  `idempotency_key`. Se serializan deliveries concurrentes de un mismo swap.
- Un `swap-settled` duplicado no cambia `remaining` ni produce otro outbox.
- Un `swap-proposed` o HTTP 202 mantiene `HOLD` sin difundir venta.
- Rechazo, expiración y cancelación liberan `HOLD` sin descontar `remaining`.
- La liquidación después de `lastOfferAt` aplica el hecho económico, pero no
  publica un `offer-updated` fuera de ventana.
- En ventas independientes de la misma oferta, cada liquidación válida
  descuenta su propia reserva y envía el `remaining` confirmado.

## Evidencia

Prueba offline: `infra/cdk/tests/test_e3043_swap_settlement_lock.py`.
Pruebas PostgreSQL: `master/tests/test_e3043_seller_offer_settlement_db.py`.
Regresión E2: `master/tests/test_seller_offer_events.py`.

Las pruebas no implican ejecución real en AWS, Wallet o RabbitMQ. Mantener el
feature flag de saga apagado hasta completar E3-044 a E3-048 y la integración
end-to-end.
