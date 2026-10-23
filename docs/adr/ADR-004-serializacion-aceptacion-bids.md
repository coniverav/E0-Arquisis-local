## AD4: Serialización de la aceptación de bids

### Contexto

En la E2, las ciudades publican ofertas de energía y reciben *bids* de otros compradores mediante RabbitMQ. Cada oferta mantiene una cantidad disponible (`remaining`), que debe considerar la energía ya comprometida. Al aceptar un bid, el vendedor propone un *swap* en la wallet, que retiene los kWh hasta que la operación se liquide, cancele o expire.

Si llegan bids casi simultáneos cuya suma supera el `remaining`, dos procesos podrían consultar la misma disponibilidad y comprometer energía dos veces. Esto es especialmente relevante porque las escrituras a la wallet son asíncronas: un HTTP `202` no garantiza que la retención haya ocurrido. Para cumplir AD4 y RF03, la aceptación debe serializarse por oferta sin depender de la memoria de una sola instancia, dado que existen `master`, `master2` y un worker independiente.

**Escenario de atributo de calidad**

- **Fuente:** Dos o más compradores.
- **Estímulo:** Llegan bids con 40 ms de diferencia cuya cantidad conjunta supera el `remaining` de una oferta.
- **Ambiente:** Mercado abierto y wallet con su latencia habitual.
- **Artefacto:** Procesamiento de bids y propuesta de swaps.
- **Respuesta:** Se aceptan las cantidades que pueden cubrirse y se rechazan las restantes mediante `bid-rejected`, con `available` cuando corresponda, sin comprometer dos veces la misma energía.
- **Medida:** Ningún `swap-rejected` por `INSUFFICIENT_ASSET` en `KWH` atribuible a una doble aceptación propia durante el período de puntaje.

### Alternativas evaluadas

1. **Procesar bids sin coordinación adicional:** Cada instancia consulta el `remaining`, valida la solicitud y propone el swap directamente a la wallet.
   1. **Ventajas:**
      1. Implementación sencilla y sin nuevas estructuras de sincronización.
      2. Permite procesar bids de forma concurrente.
      3. Reduce la lógica de coordinación del backend.
   2. **Desventajas:**
      1. Dos procesos pueden leer el mismo `remaining` antes de actualizarlo.
      2. Existe riesgo de comprometer dos veces la misma energía.
      3. La wallet podría rechazar swaps por decisiones concurrentes propias.
      4. No responde adecuadamente al escenario de AD4.

2. **Utilizar un mutex en memoria por oferta:** Cada bid debe obtener un bloqueo local asociado al `offerId` antes de revisar la disponibilidad.
   1. **Ventajas:**
      1. Implementación relativamente simple, sin agregar otro servicio.
      2. Evita procesamiento simultáneo dentro de una misma instancia.
      3. Permite procesar ofertas distintas de manera independiente.
   2. **Desventajas:**
      1. Los bloqueos no se comparten entre `master` y `master2`.
      2. Se pierden al reiniciar el proceso.
      3. No permiten recuperar por sí solos una aceptación pendiente.
      4. Exigen coordinación adicional al trabajar con múltiples instancias.

3. **Bloqueos transaccionales y reservas persistidas en PostgreSQL:** El backend bloquea la oferta mediante `SELECT FOR UPDATE`, verifica el `remaining` teniendo en cuenta los compromisos vigentes y registra una reserva y la intención de envío.
   1. **Ventajas:**
      1. Coordina aceptaciones entre instancias del backend.
      2. Impide que dos transacciones comprometan simultáneamente la misma cantidad.
      3. Las reservas sobreviven a reinicios y pueden auditarse.
      4. Reutiliza PostgreSQL y admite procesamiento asíncrono con un worker.
      5. Limita la contención a la oferta correspondiente, sin detener todo el mercado.
   2. **Desventajas:**
      1. Requiere estados y registros de reserva adicionales.
      2. Añade transacciones y bloqueos a la aceptación.
      3. Las reservas deben confirmarse o liberarse según los eventos de la wallet.
      4. Las respuestas inciertas requieren reconciliación; un error podría dejar reservas pendientes innecesariamente.

4. **Cola de procesamiento exclusiva por oferta:** Los bids se almacenan como trabajos y un consumidor procesa secuencialmente los de cada oferta.
   1. **Ventajas:**
      1. Hace explícito el orden de procesamiento.
      2. Desacopla la recepción del bid de la decisión de aceptación.
      3. Facilita limitar la concurrencia por oferta.
   2. **Desventajas:**
      1. Requiere encolamiento, recuperación y coordinación de consumidores.
      2. No elimina la necesidad de persistir reservas.
      3. Puede aumentar la latencia; un consumidor detenido retrasa su oferta.

### Decisión

**Utilizar bloqueos transaccionales por oferta y reservas persistidas en PostgreSQL** (alternativa 3). Ante un bid válido, el backend comprobará su identificador de idempotencia, bloqueará la oferta, verificará la disponibilidad considerando reservas pendientes y registrará el compromiso local junto con la intención durable de proponer el swap.

La transacción terminará **antes** de la solicitud HTTP a la wallet, para no mantener el bloqueo durante su latencia. Un proceso asíncrono propondrá el swap con una `Idempotency-Key` persistida. El HTTP `202` no confirmará la retención: solo después de `swap-proposed` se enviará `bid-accepted` al comprador.

Ante `swap-rejected`, la reserva se liberará y se enviará el `bid-rejected` correspondiente. Si la respuesta HTTP es incierta, se mantendrá la reserva hasta consultar o conocer el resultado. Tras `swap-settled`, se registrará la venta y se actualizará el `remaining`.

Así, PostgreSQL coordina las aceptaciones locales, mientras la wallet mantiene la autoridad sobre los kWh efectivamente retenidos y transferidos (PostgreSQL Global Development Group, s. f.).

### *Tradeoff*

La decisión privilegia consistencia, coordinación entre instancias y recuperación por sobre la simplicidad del procesamiento concurrente. Los mutex en memoria son más sencillos, pero no coordinan las réplicas ni conservan compromisos ante reinicios.

A cambio, hay que mantener reservas, sus estados y la reconciliación de operaciones pendientes; además, los bids de una misma oferta pueden experimentar cierta espera. El bloqueo se limita a una transacción local corta y no permanece abierto mientras responde la wallet, reduciendo la contención.

### Bibliografía

PostgreSQL Global Development Group. (s. f.). *13.4. Data Consistency Checks at the Application Level*. PostgreSQL Documentation. Recuperado de https://www.postgresql.org/docs/16/applevel-consistency.html

PostgreSQL Global Development Group. (s. f.). *13.2. Transaction Isolation*. PostgreSQL Documentation. Recuperado de https://www.postgresql.org/docs/16/transaction-iso.html

Pontificia Universidad Católica de Chile. (2026). *2026-2 / IIC2173 - E2 | EnergyShark*. Enunciado del curso.
