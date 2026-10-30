# Contratos v3 vendorizados para pruebas (E2-022 / E2-028 / E2-040–044)

Estos archivos son copias locales que permiten ejecutar la suite sin clonar
otro repositorio y sin depender de la red.

## Schemas

- Origen: [`iic2173/energyshark-contracts`](https://github.com/iic2173/energyshark-contracts)
  tag `v3.0.2`, commit `62f6cf40768b6e42717d126e528dcd944e5bc0e3`.
- Son copias sin modificaciones. `SOURCE_SHA256.json` guarda el SHA-256 de
  cada archivo y `test_contracts_v3.py` lo verifica.
- Se incluyen los 46 schemas oficiales: mensajes AMQP, wallet, token,
  catálogos de razón, fragmentos comunes e índice. El repositorio
  `Arquisis-G10-Contracts` conserva también sus fixtures oficiales.

## Ejemplos

- Los archivos locales de `examples/` son **ejemplos sintéticos propios**
  conservados de pruebas anteriores; no son fixtures oficiales del curso.
  Los 261 fixtures oficiales v3.0.2 están en el repositorio
  `Arquisis-G10-Contracts/docs/examples/v3/`.
- `status-statement.json` es el ejemplo completo: incluye `energy.sellable`,
  `market.buybackPrice`, `minBid`, `imbalancePrice`, tiempos y límites.
- `wallet-balance-response.json` incluye `assets.CR/KWH`, `asOf`,
  `lastSettledCycle` y `balanceAtLastClose`.
- `swap-settled.json` incluye `balancesAfter` con `CR`/`KWH` y sin
  `creditLine`.
- `offer.json` usa `offerId: KLD-9431`; el schema sólo exige un string no
  vacío, así que `KLD-cycle-9431` también es válido.
- `swap-token-claims.json` describe los claims ES256 que la wallet firma para
  cada swap; la firma y el `kid` se validan en tests, no en JSON Schema.
- `trade-recorded.json` e `imbalance-settlement.json` cubren los hechos que la
  central publica después del swap y al cierre del mercado.
- `wallet-statement-response.json` es una página de `GET /statement` con
  `entries` y `nextCursor`; `wallet-jwks.json` es el JWKS de la wallet.
- `wallet-propose-swap-request.json` y `wallet-write-accepted-response.json`
  fijan la forma cerrada de `POST /swaps` y de su `202`.

## Actualización

Al cambiar el tag oficial:

1. Copiar todos los schemas desde el tag nuevo.
2. Regenerar `SOURCE_SHA256.json`.
3. Ejecutar `python -m pytest tests/test_contracts_v3.py`.
4. Revisar si cambió algún `required` o `additionalProperties` y ajustar los
   parsers y tests.
