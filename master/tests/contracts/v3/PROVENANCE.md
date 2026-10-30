# Schemas v3 fijados para pruebas locales

Esta carpeta contiene sólo los schemas que usan los ejemplos de la suite de
Master y sus referencias transitivas. Permite ejecutar las pruebas sin red.
El inventario completo, los fixtures oficiales y el OpenAPI pertenecen a
[`Arquisis-G10-Contracts`](https://github.com/VicenteIgnacioSotoGonzalez/Arquisis-G10-Contracts).

## Schemas

- Origen: [`iic2173/energyshark-contracts`](https://github.com/iic2173/energyshark-contracts)
  tag `v3.0.2`, commit `62f6cf40768b6e42717d126e528dcd944e5bc0e3`.
- Son copias sin modificaciones. `SOURCE_SHA256.json` guarda el SHA-256 de
  cada archivo y `test_contracts_v3.py` lo verifica.
- Se conservan 21 schemas necesarios para los ejemplos que Master valida;
  no representan el inventario completo ni se cargan en producción.
  Los 46 schemas oficiales están en `Arquisis-G10-Contracts/docs/schemas/v3/`.

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

1. Copiar desde el tag nuevo los schemas usados por estas pruebas y sus
   referencias transitivas. Actualizar el inventario completo en Contracts.
2. Regenerar `SOURCE_SHA256.json`.
3. Ejecutar `python -m pytest tests/test_contracts_v3.py`.
4. Revisar si cambió algún `required` o `additionalProperties` y ajustar los
   parsers y tests.
