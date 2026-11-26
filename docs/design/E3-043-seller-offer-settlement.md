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

## E3-051 — Publicación de ofertas por relay

Los productores de `offer`, `offer-updated` y `offer-withdrawn` reutilizan
`outbound_messages`: negocio e intención se confirman en una sola transacción.
El relay sólo obtiene filas confirmadas y publica el envelope v3 almacenado;
un replay conserva `msgId`, `idpk`, precio y cantidad. No crea otra intención.
La confirmación positiva del broker precede a `PUBLISHED`. Si se pierde esa
confirmación o su registro local, el lease permite repetir el mismo mensaje:
la entrega es al menos una vez y requiere deduplicación por el receptor.

El orden se conserva por ciclo (una oferta propia por ciudad/ciclo): una fila
pendiente o reclamada bloquea sus actualizaciones y retiro posteriores. Cada
actualización refleja el `remaining` confirmado al crearla; no se reescribe un
mensaje histórico con el saldo de hoy. La última actualización vigente converge
al saldo PostgreSQL. Precio, retiro, reserva de bids y settlement bloquean y refrescan la oferta
para evitar reutilizar objetos obsoletos de la sesión. Los HOLD reducen capacidad
disponible para nuevos bids, pero sólo `swap-settled` descuenta `remaining`.
Retirar una oferta no libera reservas ni cancela swaps ya aceptados.

Se conserva la cuota compartida `market:offer` configurada por el estado oficial
para los tres tipos. Una cuota agotada reagenda la misma intención. `lastOfferAt`,
`closesAt`, `market-closed` y la expiración del outbox impiden nuevos despachos;
el cierre también invalida claims pendientes. El connector comprueba expiración
y lease inmediatamente antes de publicar. Un cierre recibido cuando el envío ya
está en vuelo no puede deshacer la entrega al broker: no se garantiza recepción
por Central dentro de ventana ni ausencia de sanciones tras el corte.

### Cutover reversible, desactivado por defecto

`E3_OWN_OFFERS_OUTBOX_ENABLED=false` mantiene las ofertas en el dispatcher E2.
Con `true`, todas las réplicas API excluyen ofertas de la lista E2, rechazan sus
reservas de cuota/resultados E2 y habilitan su claim E3. No activar en una mezcla
de réplicas con valores distintos. El perfil local aislado E3-079 lo activa
explícitamente; la configuración de producción no cambia.

Para activar, detener y drenar los publicadores existentes (incluidos lotes
obtenidos antes del cambio), configurar el flag en todas las réplicas API y
arrancar el connector con `E3_BROKER_OUTBOX_RELAY_ENABLED=true`. Este último flag
selecciona el relay para **todo** el despacho BROKER: coordinar también el flag
de reportes E3-050 si hay reportes pendientes, para no dejarlos sin publicador.
No ejecutar los dos bucles sobre las mismas filas. Para revertir, detener y
drenar el relay, desactivar el flag en todas las réplicas y volver al dispatcher
E2. Los claims aún vigentes se respetan; al vencer, la ruta E2 recupera las filas
sin cambiar su envelope. Una publicación aceptada cuyo confirm se perdió puede
repetirse también durante el rollback.

Validación específica: `master/tests/test_e3051_own_offers_outbox.py`, junto con
las suites de E3-028, E3-043, relay E3-029 y mercado E2. Se usan PostgreSQL efímero,
Wallet simulada y confirmaciones AMQP simuladas; no certifica integración con
servicios productivos.
