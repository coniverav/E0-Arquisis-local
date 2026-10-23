## AD5: Consistencia entre el ledger local y la wallet

### Contexto

En la E1, el ledger de PostgreSQL permitía reconstruir los balances de cada ciclo y consultar su estado materializado. En la E2, la **wallet de la central es la fuente de verdad** de los saldos `CR` y `KWH`, sus retenciones y las operaciones de swap; el ledger local pasa a apoyar el historial, la auditoría y las decisiones del mercado.

Las escrituras a la wallet son asíncronas: una solicitud puede responder HTTP `202`, quedar sin respuesta o resolverse después. Asimismo, los eventos pueden repetirse o llegar desordenados; por ejemplo, `trade-recorded` puede preceder a `swap-settled`. Para cumplir AD5, RF03 y RF05, el nodo debe aplicar cada efecto económico una sola vez y detectar divergencias entre su estado local y los saldos oficiales.

**Escenario de atributo de calidad**

- **Fuente:** Wallet de la central.
- **Estímulo:** Se recibe un `swap-settled` duplicado, otro después de su `trade-recorded` y un evento de una operación cuya respuesta HTTP `202` nunca llegó.
- **Ambiente:** Operación normal del mercado.
- **Artefacto:** Procesamiento de eventos de wallet y actualización del ledger local.
- **Respuesta:** Cada efecto económico se aplica una sola vez, sin depender del orden de llegada, y se detectan diferencias con la wallet.
- **Medida:** Al cierre de cada ciclo, el dashboard coincide con `GET /balance` o permite explicar las diferencias con registros propios.

### Alternativas evaluadas

1. **Mantener el ledger local como fuente de verdad:** Calcular saldos a partir de mensajes y operaciones guardados en PostgreSQL, usando la wallet principalmente para ejecutar swaps.
   1. **Ventajas:**
      1. Reutiliza gran parte del diseño de E1.
      2. Permite consultar balances sin solicitudes frecuentes a la wallet.
      3. Conserva un historial propio de efectos procesados.
   2. **Desventajas:**
      1. Contradice la autoridad económica definida para E2.
      2. Puede desconocer operaciones ya ejecutadas por la wallet.
      3. Eventos perdidos, desordenados o respuestas HTTP inciertas pueden causar divergencias.
      4. Carece de una comprobación independiente del saldo oficial.

2. **Consultar la wallet sin mantener ledger local:** Usar `GET /balance` para los saldos y `GET /statement` para los movimientos oficiales.
   1. **Ventajas:**
      1. Consulta directamente la fuente de verdad.
      2. Evita mantener un saldo económico independiente.
      3. Simplifica la actualización del estado mostrado.
   2. **Desventajas:**
      1. Depende de la disponibilidad y latencia de la wallet.
      2. Aumenta lecturas REST y exposición a límites de solicitudes.
      3. Dificulta correlacionar movimientos con bids, ofertas y decisiones locales.
      4. No elimina la necesidad de registrar operaciones pendientes y mensajes duplicados.

3. **Actualizar el ledger exclusivamente mediante eventos de la wallet:** Procesar eventos como `swap-proposed`, `swap-settled` y `trade-recorded`, identificando los efectos ya aplicados.
   1. **Ventajas:**
      1. Aprovecha la arquitectura orientada a eventos de E1.
      2. Reduce la necesidad de consultar la wallet después de cada operación.
      3. Facilita un historial *append-only* correlacionado con swaps y ciclos.
   2. **Desventajas:**
      1. La idempotencia no recupera por sí sola eventos perdidos.
      2. Los retrasos pueden dejar el estado local desactualizado.
      3. No detecta todas las divergencias contra la wallet.
      4. Necesita recuperación de operaciones cuyo resultado HTTP es incierto.

4. **Ledger local idempotente con conciliación periódica contra la wallet:** Persistir eventos y efectos económicos sin duplicarlos, contrastándolos con los saldos y asientos oficiales.
   1. **Ventajas:**
      1. Respeta a la wallet como fuente de verdad y conserva el historial E1.
      2. Evita reaplicar efectos de eventos duplicados.
      3. Permite procesar `swap-settled` y `trade-recorded` sin depender de su orden.
      4. Detecta diferencias y ayuda a recuperar operaciones inciertas.
      5. Reutiliza PostgreSQL y el worker existentes.
   2. **Desventajas:**
      1. Requiere conciliación, estados intermedios y recuperación de información faltante.
      2. Aumenta las lecturas a la wallet, con control de límites y reintentos.
      3. La consistencia local es eventual y puede haber diferencias temporales.
      4. Los ajustes de conciliación deben conservar evidencia y explicación.

### Decisión

**Mantener un ledger local idempotente con conciliación periódica contra la wallet** (alternativa 4). PostgreSQL conservará eventos, operaciones y movimientos para reconstruir el historial y apoyar decisiones; la wallet seguirá siendo la autoridad de los saldos `CR` y `KWH`.

Cada evento se registrará con una identidad que distinga su operación y su efecto económico. La aplicación del efecto y el registro de que fue procesado se realizarán en la misma transacción para evitar duplicaciones. `swap-settled` y `trade-recorded` se tratarán como hechos diferentes de un mismo `swapId`: intercambio de activos y cargo de transporte, respectivamente, sin importar el orden de llegada.

Los `balancesAfter` recibidos se conservarán como evidencia, sin sobrescribir indiscriminadamente un estado más reciente. Ante una respuesta HTTP incierta, se mantendrá la operación pendiente con su `Idempotency-Key`, resolviéndola mediante eventos, consultas del swap o un reintento con la misma llave. Un HTTP `202` no se interpretará como liquidación.

Un proceso consultará periódicamente `GET /balance` y utilizará `GET /statement` para examinar diferencias o recuperar movimientos. La comparación tendrá en cuenta saldos disponibles y retenidos y la vigencia temporal de las respuestas. Al cierre de cada ciclo, el dashboard reflejará el estado oficial conocido o identificará discrepancias sin corregir silenciosamente el historial (Microsoft, 2026; PostgreSQL Global Development Group, s. f.).

### *Tradeoff*

La decisión privilegia consistencia con la wallet, recuperación y auditabilidad por sobre simplicidad. Mantener el ledger de E1 como autoridad local no se ajustaría al nuevo modelo; consultar solamente la wallet reduciría duplicación de estado, pero sacrificaría autonomía e historial propio.

Se acepta **consistencia eventual** y el costo de mantener estados pendientes, consultas REST y lógica de conciliación. A cambio, el sistema conserva trazabilidad y evita confundir una operación HTTP aceptada con una operación efectivamente liquidada.

### Bibliografía

Microsoft. (2026). *Patrón de consumidor idempotente*. Microsoft Learn. Recuperado de https://learn.microsoft.com/azure/architecture/patterns/idempotent-consumer

Microsoft. (s. f.). *Implement the Transactional Outbox pattern by using Azure Cosmos DB*. Microsoft Learn. Recuperado de https://learn.microsoft.com/en-us/azure/architecture/databases/guide/transactional-out-box-cosmos

PostgreSQL Global Development Group. (s. f.). *3.4. Transactions*. PostgreSQL Documentation. Recuperado de https://www.postgresql.org/docs/16/tutorial-transactions.html

Pontificia Universidad Católica de Chile. (2026). *2026-2 / IIC2173 - E2 | EnergyShark*. Enunciado del curso y Referencia de la API v3.
