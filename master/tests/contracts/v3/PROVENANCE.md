# Contratos v3 vendorizados para pruebas (E2-022 / E2-028)

Estos archivos son copias locales que permiten ejecutar la suite sin clonar
otro repositorio y sin depender de la red.

## Schemas

- Origen: [`iic2173/energyshark-contracts`](https://github.com/iic2173/energyshark-contracts)
  tag `v3.0.1`, commit `6cae6dcf1be954eb80c28fa34af52b1fcadf91de`.
- Son copias sin modificaciones. `SOURCE_SHA256.json` guarda el SHA-256 de
  cada archivo y `test_contracts_v3.py` lo verifica.
- Se incluye sólo el cierre de `$ref` necesario para `status-statement`,
  `offer`, `swap-settled` y `wallet/balance` (14 schemas).

## Ejemplos

- Origen: `Arquisis-G10-Contracts`, `docs/examples/v3/valid/`.
- Ese repositorio los declara **ejemplos sintéticos propios**, no fixtures
  oficiales del curso; se usan porque validan contra los schemas oficiales.
- `status-statement.json` es el ejemplo completo: incluye `energy.sellable`,
  `market.buybackPrice`, `minBid`, `imbalancePrice`, tiempos y límites.
- `wallet-balance-response.json` incluye `assets.CR/KWH`, `asOf`,
  `lastSettledCycle` y `balanceAtLastClose`.
- `swap-settled.json` incluye `balancesAfter` con `CR`/`KWH` y sin
  `creditLine`.
- `offer.json` usa `offerId: KLD-9431`; el schema sólo exige un string no
  vacío, así que `KLD-cycle-9431` también es válido.

## Actualización

Al cambiar el tag oficial:

1. Copiar los mismos 14 schemas desde el tag nuevo.
2. Regenerar `SOURCE_SHA256.json`.
3. Ejecutar `python -m pytest tests/test_contracts_v3.py`.
4. Revisar si cambió algún `required` o `additionalProperties` y ajustar los
   parsers y tests.
