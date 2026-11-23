# E3-039 — Reserva y rechazo seller sobre AD4

## Decisión de integración

La operación económica ocurre **antes** de iniciar Step Functions:
`reserve_incoming_bid` bloquea la oferta, verifica `market.minBid`, disponibilidad
(`remaining_quantity - SUM(HOLD)`), persiste `MarketBid`, `OfferReservation(HOLD)`,
`SwapRecord`, `SagaExecution` y `SAGA_START` en la transacción del ingress.
Por ello `ReserveEnergy` **no vuelve a reservar**: recupera y verifica estas filas.
Crear otro HOLD desde Lambda rompería AD4 e introduciría doble venta.

Los rechazos `BID_TOO_SMALL` y `QUANTITY_UNAVAILABLE` se emiten desde el
backend con `bid-rejected` en outbox, en el mismo commit del bid rechazado.
No se registra `SAGA_START`, por lo que no existe una ejecución seller adicional.
La Lambda `reject-seller-bid` es **read-only** e inspecciona el rechazo ya
persistido. No publica directamente en RabbitMQ ni inventa otra respuesta.

## Adaptador y protección ante reintentos

- `infra/cdk/lambda_src/saga_task/seller_reservation.py` implementa las dos
  verificaciones PostgreSQL sin ORM, AWS o Wallet; todas las consultas usan
  parámetros, nunca interpolación SQL.
- `ReserveEnergy` verifica identidad de oferta/bid/ejecución, `HOLD` único,
  `minBid`, monto congelado en `SwapRecord`, intención `SAGA_START` y la
  invariante `SUM(HOLD) <= remaining_quantity`.
- Se bloquea `market_offers` antes de `market_bids` para mantener el orden de AD4.
- Si una reserva desaparece, el precio no coincide o hay sobreventa, falla
  cerrado (`SellerReservationInconsistent`): no se libera energía ni se crea
  un segundo swap.
- Se ajusta el `Catch` ASL a `SellerInsufficientEnergy`, nombre de excepción
  Python real, para el caso excepcional de una ejecución que encuentre un
  rechazo por capacidad ya persistido. El flujo normal rechaza antes de crear
  la ejecución.

## Ejecución y límites pendientes

- No se activa `E3_SELLER_SAGA_ENABLED` en producción.
- E3-040 todavía debe adaptar el payload real de `POST /swaps` y los
  callbacks. E3-041–043 completan publicación, terminales y compensación.
- La máquina ASL aún tiene tareas de negocio sin implementar. Estas pruebas
  **no** demuestran un swap `SETTLED` en AWS.
- Probar antes del cutover el caso de inicio tardío después de `closesAt`
  (E3-048) y el manejo de reconciliación de Wallet (E3-047).

## Pruebas locales

```bash
python -m unittest discover -s infra/cdk/tests -p 'test_e3039*.py' -v
cd master
python -m pytest -v tests/test_e3039_seller_tasks_db.py \
  tests/test_e3038_seller_saga_start.py tests/test_e3027_seller_saga_db.py
```

La suite de PostgreSQL necesita `DATABASE_URL` de una base temporal con nombre
`energyshark_test_*`. No usa AWS ni RabbitMQ reales.
