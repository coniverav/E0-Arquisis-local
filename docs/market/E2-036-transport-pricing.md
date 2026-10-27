# E2-036 · Transporte comprador y precio efectivo

- Para el ciclo consultado, seleccionar la `distance-table` de la ciudad compradora
  con `cycleId` coincidente y el `timestamp` más reciente (desempate por recepción/ID).
- `distance` está en **metros** y `transportCost` está en **CR / (kWh·km)**.
- `transportPerEnergy = round2(distance / 1000 * rate)` usando decimales,
  con redondeo `ROUND_HALF_UP` a dos decimales.
- `effectivePricePerEnergy = round2(pricePerEnergy + transportPerEnergy)`.
- Si `enabled` es `false`, usar `disabledRouteTransportCost` oficial, **no**
  `transportCost` de la ruta (la compra se permite, con penalización).
- Si todavía falta tabla vigente, ruta o tarifa punitiva, dejar precio efectivo
  como `null` en lugar de mostrar una cifra inventada.
- La central calcula el cargo final al comprador al liquidar y lo envía en
  `trade-recorded`. El cálculo aquí es informativo / de selección, no saldo wallet.

El endpoint `GET /market/book?cycleId=...` (E2-035) mantiene su orden nominal.
La selección de E2-037 usará los campos de transporte sin autocomprar.
