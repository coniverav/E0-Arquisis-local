# AI Log - Vicente Soto

# E2-013 - Consumir cola `market.{CODE}` además de `city.{CODE}`

## 1. Comprensión de la tarjeta y del requisito

### Prompt relevante

> "Quiero implementar exclusivamente la tarjeta E2-013: Consumir cola `market.{CODE}` además de `city.{CODE}`."

También se indicó que debían mantenerse los consumidores de E1, evitar declaraciones de topología, no enviar ACK/NACK de protocolo para el feed y no implementar todavía publicación de ofertas, lógica comercial, wallet, swaps ni cambios del frontend.

### Resumen de la respuesta y utilidad

La revisión permitió separar el alcance de E2-013 en cuatro responsabilidades: consumir ambas colas, persistir el feed antes del ACK interno de RabbitMQ, aislar los consumidores y asegurar reconexión y shutdown. También permitió distinguir estas tareas de las tarjetas posteriores de mercado, wallet y mensajes dirigidos v3.

La configuración utilizada como referencia fue:

```text
RABBITMQ_QUEUE=city.KLD.q
RABBITMQ_MARKET_QUEUE=market.KLD
CITY_ID=KLD
```

---

## 2. Implementación del segundo consumidor

### Prompt relevante

> "Mantener el consumidor actual de `RABBITMQ_QUEUE` e incorporar un segundo consumidor utilizando `RABBITMQ_MARKET_QUEUE`, con conexiones lógicamente aisladas, reconexión automática y shutdown limpio."

### Resumen de la respuesta y utilidad

La respuesta se concretó en un segundo worker para la cola de mercado, una conexión y canal independientes, obtención de la cola sin declarar topología, reconexión ante errores y cancelación coordinada de los cuatro workers.

Se mantuvo `consume_forever()` para `RABBITMQ_QUEUE` y se agregó `consume_market_forever()` en `connector/connector.py`.

El consumidor de mercado utiliza una conexión y un canal independientes, configura QoS, obtiene `RABBITMQ_MARKET_QUEUE` sin declarar topología y reintenta la conexión ante errores. El consumidor de ciudad no fue reemplazado ni se modificaron sus reglas E1 de envelope, identidad, ACK/NACK, auditoría y publicación de respuestas.

La obtención de la cola utiliza `channel.get_queue(RABBITMQ_MARKET_QUEUE, ensure=False)` para no declarar, modificar ni eliminar colas, exchanges o bindings administrados por el curso.

El apagado de `main()` cancela y espera los cuatro workers: `heartbeat`, consumidor de ciudad, consumidor de mercado y publicador de outbox. También se instalaron manejadores de `SIGTERM` y `SIGINT` para permitir un cierre ordenado.

---

## 3. Validación y persistencia del feed

### Prompt relevante

> "Para el feed del mercado no enviar ACK/NACK de protocolo y confirmar la entrega RabbitMQ solamente después de que el mensaje haya sido procesado o entregado de forma segura al mecanismo interno correspondiente."

### Resumen de la respuesta y utilidad

Se implementó una validación mínima del envelope y de la identidad AMQP, seguida de persistencia en el inbox del master. Esto permitió confirmar que la recepción del feed queda separada de las respuestas ACK/NACK del protocolo y que los eventos válidos no pasan por las validaciones comerciales E1 todavía incompletas.

Se creó `connector/protocol/market.py`. La validación es deliberadamente mínima: comprueba el envelope común, UUIDs, timestamp con zona horaria, `cycleId`, tipos permitidos y la correspondencia entre `cityId` y la propiedad AMQP `user_id` para ofertas.

Los tipos aceptados son:

```text
offer
offer-updated
offer-withdrawn
market-closed
```

Los mensajes válidos se envían a `POST /internal/audit/inbound/market`. La ruta utiliza `record_inbound_message()` y persiste el mensaje con estado `RECEIVED` antes de que el connector confirme la entrega RabbitMQ. El endpoint conserva campos adicionales del feed para el procesamiento futuro, pero no aplica ofertas ni cambia el ledger. Los mensajes de mercado no generan respuestas ACK/NACK del protocolo.

Si el backend no confirma la persistencia, la entrega se reencola. Un mensaje de feed inválido se audita mediante la ruta existente y solo se rechaza definitivamente después de guardar esa evidencia.

---

## 4. Configuración de Compose

Se agregó `RABBITMQ_MARKET_QUEUE` al servicio `connector` en `docker-compose.yml` y `docker-compose.prod.yml`.

---

# E2-014 - Separar publicación del feed de mercado y mensajes dirigidos

## 1. Comprensión de la tarjeta

### Prompt relevante

> "Quiero implementar exclusivamente la tarjeta **E2-014: Separar publicación del feed de mercado y mensajes dirigidos**, correspondiente a G02 de EnergyShark E2."

El pedido precisó los destinos de `offer`, `offer-updated`, `offer-withdrawn`, `bid`, `bid-accepted`, `bid-rejected` y mensajes vigentes hacia la central. También pidió reutilizar el outbox, conservar E2-013 y E1 vigente, y no publicar pruebas en el broker del curso.

### Resumen de la respuesta y utilidad

La revisión mostró que el outbox E1 conservaba la `routingKey`, pero el connector publicaba sus mensajes únicamente en `energy.x`. Se consultaron la referencia E2-009 y los schemas oficiales del curso en el commit `6cae6dcf1be954eb80c28fa34af52b1fcadf91de` para identificar el exchange y la routing key de cada tipo.

---

## 2. Enrutamiento y persistencia del destino

### Prompt relevante

> "Comprueba que el destino de cada mensaje —exchange y routing key— quede correctamente asociado a la operación y se conserve durante sus reintentos."

### Resumen de la respuesta y utilidad

Se agregó `connector/protocol/outbound.py` para seleccionar y comprobar el destino antes de publicar:

```text
offer, offer-updated, offer-withdrawn → market / market.offer.KLD
bid, bid-accepted, bid-rejected → energy.x / city.{DESTINATARIO}
request, negotiation-report → energy.x / central
ACK/NACK dirigidos → energy.x / central o city.{EMISOR}
```

`connector/connector.py` obtiene ambos exchanges sin declarar topología y mantiene `user_id=city.KLD`. Los ACK/NACK de mensajes dirigidos a otra ciudad se encaminan a la identidad AMQP de esa ciudad. La recepción independiente de `city.KLD.q` y `market.KLD` de E2-013 se conservó.

El modelo `OutboundMessage`, su servicio de auditoría y los endpoints internos guardan y devuelven `exchange_name` junto con `routing_key`. La migración `master/alembic/versions/e214_outbound_exchange.py` asigna `energy.x` a las filas E1 existentes. Así, un reintento utiliza el destino persistido. También se pasó `RABBITMQ_MARKET_EXCHANGE` al connector en ambos archivos Compose.

---

## 3. Pruebas y límites de la verificación

### Prompt relevante

> "Agrega pruebas locales utilizando mocks o fixtures para comprobar:"

### Resumen de la respuesta y utilidad

Se agregaron `connector/tests/test_outbound_routing.py` y `master/tests/test_outbound_exchange.py`. Las pruebas cubren destinos de ofertas, bids y mensajes a la central; identidad AMQP; rutas no autorizadas; persistencia del exchange; reintentos sobre el mismo destino; y ausencia de declaraciones de topología. También se ejecutaron las pruebas existentes de E1 y E2-013.

```text
Connector: 110 passed
Backend sin base de datos real: 5 passed
Backend con PostgreSQL temporal: 10 passed
```

La migración llegó a `e214outbound` en el PostgreSQL temporal, que se detuvo después de las pruebas. La configuración Compose validó con `.env.example`. No se leyó `.env`, no se publicó en el broker del curso y no se desplegó en AWS.

La publicación automática de ofertas, la lógica comercial de bids, la wallet y la validación JSON Schema completa desde Contracts permanecen fuera de esta implementación.

---

# E2-015 - Validar identidad `user_id` de mensajes entre ciudades

## 1. Comprensión de la tarjeta

### Prompt relevante

> "Quiero implementar exclusivamente la tarjeta **E2-015: Validar identidad `user_id` de mensajes entre ciudades**, correspondiente a **G02 y RF05** del enunciado E2 de EnergyShark."

El pedido exigió comparar el `user_id` AMQP con el `cityId` de mensajes dirigidos, responder `IDENTITY_MISMATCH/403` cuando correspondiera, conservar la correlación mediante `msgId` y auditar rechazos y descartes. También precisó que los eventos de la central y la wallet usan otra identificación, que el feed de mercado no lleva ACK/NACK de protocolo y que las pruebas debían ser locales, sin usar el broker de producción.

### Resumen de la respuesta y utilidad

La revisión del connector E1 mostró que ya existían parser, ACK/NACK, validación básica de identidad y auditoría, pero el catálogo del validador todavía no reconocía `bid`, `bid-accepted`, `bid-rejected` ni eventos de wallet. Se contrastaron las reglas con los schemas oficiales v3 usados en E2-009. Esto permitió acotar el cambio a la recepción dirigida y conservar el consumidor de mercado de E2-013 y el routing de E2-014.

---

## 2. Validación, respuesta y auditoría

### Prompt relevante

> "Un mensaje con `user_id` y `cityId` incompatibles debe generar NACK con `IDENTITY_MISMATCH`."

### Resumen de la respuesta y utilidad

En `connector/protocol/handler.py` se adelantó la comparación de identidad para mensajes de ciudad identificables, conservando la prioridad E1 de `IDPK_EQUALS_MSGID`. Un `bid` con identidad discordante genera NACK sin pasar al backend. Se mantuvo `MALFORMED_MESSAGE/422` para mensajes inválidos con `msgId` utilizable y el descarte sin respuesta para cuerpos no parseables o sin `msgId` válido. Los mensajes que declaran `sender: "central"` se tratan por separado; los tipos de eventos wallet dirigidos dejaron de rechazarse como desconocidos solo por ser nuevos en v3.

En `connector/connector.py`, la ruta de respuesta de una ciudad se toma del `user_id` AMQP observado, nunca del `cityId` declarado en el JSON. Si no hay una ruta autenticada a la cual responder, el mensaje se descarta y audita, sin enviarlo al procesamiento de negocio ni publicar una respuesta hacia una identidad supuesta. La auditoría de `IDENTITY_MISMATCH` conserva el `msgId` original, el motivo del NACK, el `cityId` declarado y el `user_id` observado. El ACK de RabbitMQ sigue siendo distinto del ACK/NACK del protocolo.

En `connector/protocol/validation.py` se reconocieron los tipos dirigidos v3 necesarios para esta recepción y se añadió una comprobación básica de contenido de `bid`. `connector/protocol/responses.py` limita el eco de `cycleId` en el NACK a un string de hasta 64 caracteres, según el contrato oficial. La validación completa de todos los campos v3 permanece pendiente de la integración con Contracts.

---

## 3. Pruebas locales y límites

### Prompt relevante

> "Como pruebo entonces lo que se hizo aqui?"

### Resumen de la respuesta y utilidad

Se añadió `connector/tests/test_city_identity.py` con 13 pruebas para identidad coincidente y discordante, `bid` malformado, cuerpos no parseables, ausencia de `msgId`, eventos de central y wallet, auditoría, destino de los NACK y ausencia de llamadas de negocio para mensajes rechazados. Las pruebas existentes de mercado y E1 también se ejecutaron.

```text
Pruebas nuevas de E2-015: 13 passed
Suite completa del connector: 123 passed
git diff --check: sin errores
```

Los NACK `IDENTITY_MISMATCH` y `MALFORMED_MESSAGE`, y los fixtures de `bid` y `swap-proposed`, se contrastaron localmente con los schemas oficiales v3. No se conectó al broker de producción ni se probó un despliegue real. El procesamiento comercial de bids y eventos wallet, junto con la validación JSON Schema completa desde Contracts, siguen pendientes de otras tarjetas. La ausencia de `user_id` permite detectar la identidad inválida, pero no ofrece una dirección de respuesta confiable; por eso el connector audita y descarta ese caso en vez de usar el `cityId` no verificado como destino.
