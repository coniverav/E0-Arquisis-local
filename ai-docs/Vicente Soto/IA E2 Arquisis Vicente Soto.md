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