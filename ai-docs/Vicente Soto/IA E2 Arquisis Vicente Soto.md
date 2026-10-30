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

---

# E2-016 - Gestionar ACK/NACK dirigidos y evitar respuestas infinitas

## 1. Comprensión de la tarjeta

### Prompt relevante

> "Quiero implementar exclusivamente la tarjeta **E2-016: Gestionar ACK/NACK dirigidos y evitar respuestas infinitas**, correspondiente a **G02** del enunciado E2 de EnergyShark."

Se pidió verificar la correlación y el routing de ACK/NACK para mensajes recibidos en `city.KLD.q`, evitar respuestas automáticas a `ack`, `nack` y `error`, y mantener el feed `market.KLD` sin ACK/NACK de protocolo. La tarjeta debía conservar E2-013, E2-014 y E2-015 sin incorporar lógica comercial ni cambiar topología RabbitMQ.

### Resumen de la respuesta y utilidad

La revisión del connector, las pruebas E1/E2 y los schemas oficiales v3 del commit `6cae6dcf1be954eb80c28fa34af52b1fcadf91de` mostró que `plan_protocol_response()` ya construía respuestas con `data.target` igual al `msgId` original y no respondía a `ack`, `nack` o `error`. El consumidor de mercado tampoco invocaba el publicador de respuestas. El trabajo se concentró en cerrar dos rutas de fallo y probar el flujo completo con mocks.

---

## 2. Destino autenticado y prevención de respuestas recursivas

### Prompt relevante

> "Los mensajes provenientes de otras ciudades no reciban sus respuestas en la routing key `central` por defecto."

### Resumen de la respuesta y utilidad

En `connector/connector.py`, `complete_successful_message()` dejó de sustituir una routing key ausente por `central`: ahora exige una ruta autenticada antes de persistir y publicar el ACK. El flujo de recepción continúa obteniendo `city.{CODE}` del `user_id` AMQP observado y reserva `central` para mensajes de la central identificados como tales.

En `connector/protocol/dispatch.py`, `dispatch_protocol_response()` publica únicamente cuando la acción planificada es `ack` o `nack` y el payload tiene ese mismo tipo. Una acción `ignore` o `discard` no publica, incluso si por error recibiera un payload; una acción ACK/NACK sin respuesta coherente falla antes de publicar. Se conservaron las confirmaciones internas de RabbitMQ y el registro outbound previo a la publicación.

---

## 3. Pruebas y alcance de la verificación

### Prompt relevante

> "Como yo pruebo esto que acabas de hacer?"

### Resumen de la respuesta y utilidad

Se creó `connector/tests/test_directed_ack_nack.py` y se ampliaron `connector/tests/test_connector_flow.py` y `connector/protocol/tests/test_dispatch.py`. Las pruebas simulan la entrega por la cola de ciudad y comprueban ACK/NACK correlacionados, publicación en `energy.x`, routing hacia otra ciudad o `central`, auditoría de rechazos y descartes, y ausencia de respuestas al recibir `ack`, `nack`, `error` o feed. También comprueban que la publicación del protocolo preceda al ACK interno de RabbitMQ y que un ACK sin routing key no se publique.

```text
Pruebas relacionadas: 18 passed, 5 subtests passed
Suite completa del connector: 136 passed, 5 subtests passed
git diff --check: sin errores
```

Cuatro ACK/NACK generados se validaron localmente contra los schemas oficiales v3. Las pruebas utilizaron mocks y el entorno Python existente, sin levantar Docker ni conectarse al broker de producción. Un ACK de protocolo confirma recepción y no aceptación comercial; la lógica de bids, swaps y wallet continúa fuera de esta tarjeta.

---

# E2-017 - Persistir feed de ofertas y `market-closed`

## 1. Comprensión de la tarjeta

### Prompt relevante

> "Quiero implementar exclusivamente la tarjeta **E2-017: Persistir feed de ofertas y `market-closed`**, correspondiente a **RF02 y G02** del enunciado E2 de EnergyShark."

Se pidió conservar en PostgreSQL `offer`, `offer-updated`, `offer-withdrawn` y `market-closed`, mantener un snapshot por ciudad y ciclo, deduplicar reintentos y tratar conservadoramente eventos fuera de orden. El alcance excluyó bids, wallet, estrategias, interfaz de mercado, pruebas contra el broker de producción, commits y despliegues.

### Resumen de la respuesta y utilidad

La revisión de los schemas oficiales v3 y del consumidor de E2-013 permitió reutilizar `inbound_messages` como historial de cada recepción y el endpoint interno existente como entrada del feed. El contrato no aporta un número de secuencia que permita reconstruir inequívocamente el orden de entregas; por eso se conserva la evidencia de eventos conflictivos y se marca el snapshot como incierto cuando corresponde.

---

## 2. Persistencia, deduplicación y consistencia

### Prompt relevante

> "Evitar que un mensaje duplicado aplique dos veces sus efectos."

### Resumen de la respuesta y utilidad

Se agregó la migración `e217_market_feed.py`: `offer_id` en `inbound_messages`, `market_event_claims` para reclamar `idpk` y `msgId` por emisor, `market_cycles` para registrar el cierre y `market_offers` para el snapshot único por `(cycle_id, city_id)`. `app/services/market_feed.py` guarda la recepción, la reclamación y el cambio de estado en una transacción PostgreSQL. Un reenvío queda registrado como `DUPLICATE` sin reaplicar la oferta; un identificador reutilizado con contenido distinto queda como `NOT_PROCESSED`.

La oferta registra cantidad y precio iniciales; `offer-updated` cambia `remaining` y precio; `offer-withdrawn` deja la oferta retirada; `market-closed`, emitido por la central, cierra únicamente su ciclo. Los eventos atrasados, contradictorios o posteriores al cierre no sobrescriben silenciosamente el estado. El bloqueo del ciclo serializa eventos entre las dos instancias de backend. Se ajustó la validación del feed en connector y backend para rechazar formas inválidas antes de persistirlas. No se añadieron endpoints públicos ni lógica comercial.

---

## 3. Pruebas, relación con el enunciado y límites

### Prompts relevantes

> "Ok, como pruebo esto de forma local, además, esto cumple con lo que se pide en enunciado?"

> "Probar con el broker de producción porque quedó fuera de la tarjeta?"

### Resumen de la respuesta y utilidad

Se probaron historial y snapshot, actualización de precio y `remaining`, retiro, cierre por ciclo, duplicados, separación entre ciudades y ciclos, eventos fuera de orden, rollback y concurrencia. Las pruebas del connector cubrieron ACK de entrega RabbitMQ posterior a la persistencia, reencolado ante fallo, ausencia de ACK/NACK de protocolo, reconexión y cierre. La suite completa detectó que una prueba antigua borraba `inbound_messages` sin borrar antes los nuevos claims; se corrigió su limpieza y se repitió la suite sobre una base temporal limpia.

```text
Backend con PostgreSQL temporal: 180 passed, 3 subtests passed
Connector: 137 passed, 8 subtests passed
git diff --check con CRLF reconocido: sin errores
```

Se compartieron comandos para repetir las pruebas con una base PostgreSQL desechable y se explicó cómo observar, después de un despliegue, los logs y las tablas en EC2. No se conectó esta implementación al broker de producción, no se publicaron mensajes reales y no se verificó AWS. Se distinguió que E2-017 prepara el libro de ofertas para RF02 y aporta al consumo de G02, pero RF02 completo aún exige vista, transporte, precio efectivo y rutas deshabilitadas; G02 operativo requiere comprobación en el entorno desplegado.

---

# E2-045 - Ensayo integrado v3 con MET y ciclo de prueba

## 1. Ensayo local del rol seller

### Prompt relevante

> "Por ahora quiero implementar **la primera prueba integrada del flujo seller**, sin incorporar funcionalidades pendientes de esas tarjetas."

### Resumen de la respuesta y utilidad

Se creó `master/tests/test_e2045_seller_cycle.py` para recorrer los componentes reales desde `status-statement` hasta la liquidación seller, con PostgreSQL temporal, reloj controlado, broker simulado y Wallet simulada. La prueba comprueba la oferta, recepción del bid, reserva, propuesta de swap, `swap-proposed`, `bid-accepted` y `swap-settled`, además de persistencia, outbox y correlación. La respuesta HTTP `202` deja el swap pendiente: el evento `swap-settled` es el que confirma la liquidación.

---

## 2. Ampliación al rol buyer y límites del ensayo

### Prompt relevante

> "Completes la cobertura integrada del rol buyer, reutilizando la infraestructura de pruebas."

### Resumen de la respuesta y utilidad

Se añadió `master/tests/test_e2045_buyer_cycle.py` con una oferta **sintética** de MET. El recorrido recibe el feed, selecciona una candidata y publica un bid mediante una llamada **manual** a `POST /internal/market/bids`; después procesa `bid-accepted`, aceptación HTTP de Wallet, `swap-settled`, `trade-recorded`, `market-closed`, `imbalance-settlement` y la publicación simulada de `negotiation-report`. Esto permitió probar la integración del rol comprador, pero no la generación autónoma del bid ni la aceptación del reporte por la central.

La prueba dejó visible una brecha económica: los hechos Wallet y de cierre quedaron persistidos, pero `LedgerEntry` no recibió nuevos efectos, `Cycle.budget_balance` permaneció en 0 CR y `Cycle.energy_balance` en −1000 KWH; el reporte publicado por el broker simulado reflejó esos saldos locales. El fixture no incluye financiamiento v3, por lo que sus cifras no representan un saldo productivo esperado. No se modificó lógica de negocio para ocultar el hallazgo.

---

## 3. Evidencia y alcance de la validación

### Prompt relevante

> "Registres evidencia de los escenarios ejecutados, resultados, logs y errores detectados."

### Resumen de la respuesta y utilidad

La evidencia quedó en `docs/documentacion/E2-045-evidencia-ensayo-integrado-local.md`. Allí se registraron las corridas seller y buyer (`2 passed`), regresiones relacionadas de master (`132 passed, 1 warning`) y la suite del connector (`142 passed, 8 subtests passed`). Durante la preparación se corrigieron dos errores del arnés de pruebas; las corridas finales pasaron. La advertencia corresponde a una API deprecada del entorno de tests.

La validación fue local y aislada; no se ejecutó un ciclo contra MET, RabbitMQ o Wallet reales. La ventana de sandbox indicada en el enunciado terminó el 21 de octubre y no se verificó si el servicio podía reabrirse. Por ello, esta evidencia no cumple por sí sola el criterio original de ensayo en el sandbox de MET.

---

# E2-046 - Pruebas de deduplicación feed/bid y replay wallet

## 1. Alcance de la tarjeta

### Prompt relevante

> "Una misma `idpk` no debe alterar nuevamente el ledger ni crear un segundo swap, incluso si el mensaje repetido contiene un `msgId` diferente."

### Resumen de la respuesta y utilidad

Se reutilizó el arnés seller/buyer de E2-045 y se creó `master/tests/test_e2046_dedup_replay.py`. Los casos reenvían ofertas de mercado, bids y eventos Wallet con igual `idpk` y distinto `msgId`; también prueban envelopes nuevos para el mismo `swapId` o `settlementId` y contenido conflictivo. Los mensajes pasan por los consumidores y endpoints reales, con servicios externos simulados y PostgreSQL temporal.

---

## 2. Resultados y limitaciones

### Prompt relevante

> "Verifiques que no se duplican swaps, reservas, liquidaciones ni efectos económicos persistidos."

### Resumen de la respuesta y utilidad

En los escenarios probados permanecieron un solo snapshot y claim del feed, una reserva y un swap seller, una aceptación buyer y un registro por hecho de transporte o liquidación de cierre. El replay se conservó en la auditoría de recepciones sin repetir el efecto; el contenido conflictivo no sobrescribió el snapshot. Un HTTP `202` tampoco se interpretó como settlement. La prueba verifica ausencia de duplicación sobre los efectos **actualmente implementados**; no acredita que el ledger E1 incorpore correctamente los importes Wallet.

`docs/documentacion/E2-046-evidencia-deduplicacion-local.md` registra las corridas finales: `2 passed` para los casos nuevos, `4 passed` junto a los ensayos E2-045 y `110 passed, 1 warning` en regresiones relacionadas. No se cambió lógica de negocio. Quedaron fuera la concurrencia de varias réplicas, fallas durante el commit y la interacción con broker o Wallet reales. La generación automática del bid buyer y la consistencia Wallet–ledger–reporte siguieron como asuntos separados.

---

# E2-048 - Release estable en producción

## 1. Identidad AMQP de la Central

### Prompt relevante

> "El equipo docente confirmó que el contrato oficial fue actualizado a v3.0.2 [...] Los mensajes enviados por la Central a `city.{CODE}`, incluidos los eventos Wallet, llegan con AMQP `user_id=\"central\"."

### Resumen de la respuesta y utilidad

El diagnóstico del descarte observado en EC2 condujo a la corrección `3dddfbe`, integrada en `develop` mediante la PR #73. En `connector/connector.py`, `authenticated_inbound_route()` obtiene el destino de respuesta del `user_id` AMQP; en `connector/protocol/handler.py` se admite la identidad `central` para mensajes dirigidos de la Central, y en `connector/protocol/outbound.py` se evita usar `central` como destino por defecto cuando no existe identidad autenticada. Se mantuvo la comprobación de identidad de las ciudades y el rechazo de `cityId` incompatible. Los mensajes dirigidos que declaran `sender="central"` sin `user_id` quedan descartados o reciben NACK según la ruta disponible.

Las pruebas añadidas o ajustadas cubren `status-statement`, `distance-table`, eventos Wallet, ACK/NACK hacia `central`, mensajes falsificados y respuestas que no deben generar otra respuesta. `market-closed` conserva `user_id=None` en el consumidor de mercado y no genera ACK/NACK de protocolo. También se adaptaron los fixtures integrados seller, buyer y replay al transporte v3.0.2. La evidencia proviene de pruebas locales con mocks; el resultado en EC2 después del merge no queda acreditado por estas pruebas.

---

## 2. Primer tick de conciliación Wallet

### Prompt relevante

> "El test que falló, cual fue? como se podría arreglar?"

> "Hagamos eso, creemos una rama nueva con este fix mínimo, hagamos el comit cuando funcione [...] y abramos la PR."

> "Duda, pero tocamos una funcionalidad. Entonces no sería razonable probar toda la suite de tests?"

### Resumen de la respuesta y utilidad

El test `test_reconcile_tick_throttles_between_runs` expuso que `_last_tick_monotonic=0.0` confundía «aún no ejecutado» con un instante real: si el reloj monotónico del host estaba bajo el intervalo configurado, se omitía la primera conciliación. El commit `0ad4b6c`, en la PR #74, cambió ese estado inicial a `None` en `master/app/services/wallet_reconciliation.py`. El intervalo limita los ticks posteriores, mientras que el primero se ejecuta. El test fija un reloj monotónico bajo para reproducir esa condición. No se modificó el cálculo de saldos, la aplicación de eventos ni el reporte.

Tras advertir que se había cambiado comportamiento funcional, se ejecutaron las suites completas: Master, `423 passed, 3 subtests passed`, con PostgreSQL temporal aislado; Connector, `150 passed, 12 subtests passed`. Master emitió una advertencia de deprecación de Starlette/AnyIO, sin fallos. Se actualizó la descripción de la PR con esos resultados y se retiró el contenedor temporal. Al redactar esta entrada, `develop` contiene la corrección de identidad AMQP, pero aún no incluye el commit del primer tick. La validación en producción de la conciliación y del release E2-048 sigue pendiente.

---

# E2-070 - Conciliación automática Wallet–ledger (AD5)

## 1. Diagnóstico y decisión

### Prompt relevante

> "Criterio de cierre: Wallet `GET /balance` y ledger local comparables ante reordenamientos y duplicados, con explicación por identificador y cálculo."

### Resumen de la respuesta y utilidad

El diagnóstico distinguió el saldo oficial de Wallet del ledger local y mostró que comparar solamente dos snapshots no bastaba para afirmar consistencia económica. Se precisó el ADR AD5 antes de implementar: correlacionar asientos de `/statement` con hechos locales por ciclo, swap, operación, activo y monto; conservar diferencias sin inventar una referencia local; y usar `NO_LOCAL_BASELINE` cuando falta evidencia comparable.

## 2. Implementación y corrección posterior

### Prompt relevante

> "No asumas que la ausencia de una fila `Cycle` demuestra automáticamente un bug. Primero revisa los invariantes del modelo, las condiciones que generan snapshots `EVENT` y la semántica real de los estados de conciliación."

### Resumen de la respuesta y utilidad

`master/app/services/wallet_reconciliation.py` compara `GET /balance`, snapshots de eventos y asientos de `/statement` con las operaciones y saldos materializados del ciclo. Persiste el resultado y una explicación con identificadores y deltas; la migración `e2070_reconciliation_provenance.py` incorpora la procedencia temporal necesaria. `master/app/services/wallet_service.py` conserva snapshots de eventos Wallet como evidencia, sin convertir HTTP `202` en liquidación.

La revisión independiente reprodujo un caso alcanzable: `swap-settled` podía guardar un snapshot `EVENT` para un `SwapRecord` cuyo `Cycle` no existía, sin crear asientos de ledger. Antes del fix, la igualdad entre ese snapshot y `GET /balance` producía un falso `MATCH`. El commit `40d8de7` exige presencia del ciclo para ese estado y devuelve `NO_LOCAL_BASELINE` en el caso huérfano. Las pruebas distinguen ese caso de un ciclo válido con ledger consistente, otro discrepante y una divergencia local sin baseline oficial suficiente. No se convirtió toda ausencia de baseline en `DIVERGENT`.

## 3. Validación y límite conocido

`master/tests/test_e2070_reconciliation.py` cubre las comparaciones por operación, asientos tardíos, eventos reordenados o duplicados y el caso huérfano. La validación final compartida de la PR pasó con PostgreSQL temporal aislado: Master, `507 passed, 3 subtests passed`; Connector, `151 passed, 12 subtests passed`; y contratos oficiales v3.0.2, `7 passed`. Los 25 tests focalizados de conciliación y auditoría también pasaron. No se verificó la conciliación contra Wallet productiva.

Quedó identificado un riesgo: un barrido completo de `/statement` que exceda 100 páginas falla sin guardar avance parcial y puede volver a fallar en el siguiente intento. No se comprobó que el historial real alcance ese límite. La medida de AD5 en el dashboard sigue pendiente en Frontend.

---

# E2-071 - Retry-After, deadlines y cuotas operacionales (RF03)

## 1. Alcance revisado

### Prompt relevante

> "Comprueba que las cuotas sean realmente compartidas entre Master, Master2, Scheduler y Connector."

### Resumen de la respuesta y utilidad

La revisión permitió separar la recuperación de respuestas HTTP perdidas, ya abordada en E2-068, del control de cupos y tiempos de E2-071. La misma operación económica conserva su `Idempotency-Key` durante reintentos; una respuesta HTTP incierta no se trata como un swap liquidado.

## 2. Implementación y pruebas

`master/app/services/operational_rate_limits.py` registra en PostgreSQL cupos por ciudad para lecturas y escrituras Wallet, ofertas de mercado y `request-ask`. La reserva usa bloqueo de fila para coordinar procesos; la migración `e2071_rate_limits_accept_retry.py` agrega el estado persistente. El cliente Wallet interpreta `Retry-After` en respuestas 429 y 503, mientras que las rutas de swaps, timeouts y publicación difieren intentos según límites y vencimientos. El Connector consulta el cupo antes de publicar ofertas y conserva el mensaje en el outbox si debe esperar.

`master/tests/test_e2071_operational_limits.py` prueba ventanas compartidas entre sesiones, bloqueo `Retry-After`, respuestas HTTP, aplazamiento sin perder el outbox y concurrencia por el último cupo. También se ampliaron pruebas de aceptación y timeouts de swaps y de publicación del Connector. La validación final de la rama incluyó las suites completas citadas en E2-070; fue local, sin llamadas a Wallet ni broker productivos. La coordinación real entre réplicas desplegadas y sus plazos requiere comprobación en el entorno E2.

---

# E2-075 - Auditoría Wallet e interciudad (RF05)

## 1. Trazas consultables

### Prompt relevante

> "Comprueba que ningún endpoint exponga JWT, `swapToken`, API keys o información sensible, incluso en campos anidados."

### Resumen de la respuesta y utilidad

Se agregaron en `master/app/routers/audit_v3.py` consultas paginadas para mensajes interciudad, eventos Wallet y conciliaciones. Permiten seguir `msgId`, `idpk`, ciclo y swap hasta registros de ledger, statement y snapshots. Las rutas requieren el JWT configurado y un `sub` no vacío. `GET /audit/anomalies` conserva la forma consumida por Frontend; `docs/api/E2-075-audit-v3.md` describe filtros y respuestas. Esto dejó evidencia consultable para `DUPLICATE`, `DISCARDED`, `NACKED` e `IDENTITY_MISMATCH`, sin afirmar que la vista de demo del Frontend estuviera terminada.

## 2. Corrección de fuga HTTP y motivo NACK

### Prompt relevante

> "Reproduce mediante HTTP el caso de `access_token` anidado."

### Resumen de la respuesta y utilidad

La prueba HTTP confirmó que el serializador anterior podía devolver `access_token` dentro de un payload malformado. El commit `84467e6` cambió `master/app/routers/message_audit.py` para exponer sólo identificadores auditados del envelope y dejar `rawPayload` en `null`; el original permanece en PostgreSQL para diagnóstico. También se recuperó `reasonCode` de los NACK salientes desde su payload persistido, limitado a códigos conocidos del protocolo. Un motivo NACK malformado no provoca error 500 ni se devuelve como credencial.

Las pruebas de `master/tests/test_e2075_audit_v3.py` envían por HTTP objetos y listas con distintos nombres de credenciales, texto crudo malformado y NACK válidos o malformados. Pasaron junto con los 25 tests focalizados y las suites completas indicadas antes. No se probó la política de acceso del gateway/Auth0 en producción. Faltan la actualización del OpenAPI organizacional y una vista Frontend para la demo RF05.

---

Los cambios de E2-070, E2-071 y E2-075 se integraron mediante la PR #86. Las pruebas anteriores acreditan el comportamiento local de esta rama, no su operación desplegada.

---

# E2-072 - Frescura y capacidad del feed de mercado

### Prompt relevante

> "E2-072 (G02): Soportar TTL/capacidad del feed de mercado, marcadores de frescura, `market-closed` y recuperación ante mensajes perdidos."

### Resumen de la respuesta y utilidad

La implementación de `connector/connector.py` avisa a Master al conectar o reconectar el consumidor de mercado, antes de reanudar el consumo. `master/app/services/market_feed.py` registra la posible brecha en ciclos abiertos y `master/app/services/market_book.py` marca como antiguas las ofertas cuyo último evento precede la reconexión, excluyéndolas de candidatas de compra. El libro expone `feedGapAt`, `lastFeedEventAt` y un estado de frescura; el cierre se confirma con `market-closed` o se aplica conservadoramente al llegar el plazo oficial. También se solicita nuevamente el contexto de `status-statement` y `distance-table` dentro del cupo de `request/ask`.

`connector/tests/test_market_consumer.py` y `master/tests/test_market_book.py` cubren los cambios locales. `docs/market/E2-072-feed-freshness.md` deja el límite explícito: el protocolo v3.0.2 no ofrece cursor ni replay de ofertas. Si el broker descarta mensajes por TTL o por el máximo de 5.000, el nodo no puede reconstruir todo el feed ni probar que una conexión continua no perdió eventos. Las métricas reales de la cola y el comportamiento tras reconexión siguen pendientes de validación en el entorno del curso.

---

# E2-073 - Configuración de estrategia comercial (RF04/AD6)

### Prompt relevante

> "E2-073 (RF04/AD6): Formulario Frontend para configurar precio, cantidad, span y políticas, mostrando validaciones y vigencia en el siguiente ciclo sin redeploy."

### Resumen de la respuesta y utilidad

En Backend se versionaron el precio directo y el tope de cantidad de ofertas seller en `master/app/services/strategy_config.py` y `master/app/services/seller_offer.py`, con migración y pruebas de edición. La corrección posterior respetó el precio directo configurado sin imponerle el piso usado para el cálculo por multiplicador. El OpenAPI de Contracts documenta los nuevos campos y la precisión del precio en centavos.

En Frontend se añadió `src/components/MarketStrategyView.jsx` para editar precio, fracción, cantidad máxima, span y políticas. La pantalla indica la versión activa y la pendiente, y comunica que los cambios se aplican al siguiente ciclo. Se agregaron tests de la lógica de estrategia y ajustes para aceptar precios directos en centavos y mostrar nombres legibles de políticas. Durante la revisión local con API y PostgreSQL de prueba se pudo abrir la pantalla y ver sus controles; eso no acredita por sí solo que una edición haya entrado en vigor en un ciclo real desplegado.

---

# E2-074 - Comparación del experimento span (RF06)

### Prompt relevante

> "E2-074 (RF06): Vista comparativa del experimento span, con métricas por ciclo y agrupadas, baseline y resultados Wallet."

### Resumen de la respuesta y utilidad

`master/app/services/cycle_metrics.py` incorpora ingresos seller y recompra al costo efectivo, además de los casos buyer, y sus pruebas cubren estos cálculos. Frontend agregó `src/components/SpanExperimentView.jsx` con detalle por ciclo, agregados por span y estado de conciliación Wallet. Se corrigió el agregado para considerar todos los ciclos disponibles y mantener los resultados agrupados al paginar el detalle.

La inspección visual local se hizo con cuatro ciclos sintéticos: dos finales de span 60 s, uno final de 120 s y uno pendiente de 120 s. La vista mostró el pendiente sin incluirlo en los importes agregados de ciclos finales. Las cifras mostradas eran datos de demostración, no resultados del mercado ni mediciones de Wallet productiva. El informe experimental con ciclos reales exigido por RDOC05 sigue siendo un trabajo distinto.

---

# E2-076 - Contratos v3.0.2 y CI (RDOC04)

## 1. Fuentes y separación de responsabilidades

### Prompt relevante

> "E2-076 (RDOC04): Completar schemas v3, fixtures y pruebas contractuales contra los contratos oficiales, integradas en CI."

> "Pero mi duda es más profunda, era necesario tener schemas en el repo de backend para esto?"

### Resumen de la respuesta y utilidad

`Arquisis-G10-Contracts` incorporó el inventario oficial v3.0.2: 46 schemas, 261 fixtures del curso, OpenAPI propia y pruebas contractuales. Su workflow valida los contratos y emisores del Backend contra los schemas oficiales. El repositorio Backend conserva sólo 21 schemas como fixtures locales necesarios para sus pruebas sin red; el commit `7bc3025` eliminó las copias que estas pruebas no usan. Se compararon los 21 archivos restantes con Contracts y sus hashes coincidieron. La documentación de procedencia explica que el inventario y su mantenimiento pertenecen al repositorio organizacional de Contracts.

## 2. Corrección del CI y validación

### Prompt relevante

> "Eso si, otra cosa nada que ver pero falló el CI en el repo de contratos. Igual no he mergeado ninguna PR."

### Resumen de la respuesta y utilidad

La primera falla del CI se debió a que `requirements-test.txt` instalaba `jsonschema` sin los validadores de formato: fechas sin zona horaria pasaban inadvertidas. El commit `5a221f3` agregó `jsonschema[format]` y una prueba que detecta la ausencia del validador `date-time`. El siguiente job expuso otra dependencia omitida: las pruebas integradas importaban `aio_pika` desde Connector, pero el workflow instalaba sólo Master. El commit `7dd7275` agregó la instalación de `connector/requirements.txt`.

Las 14 pruebas locales de Contracts pasaron y el job `contracts` de la PR #12 terminó en verde después de ambos ajustes. Antes de ello, se habían ejecutado 39 pruebas relevantes de Backend tras reducir sus fixtures. El CI de Contracts descargaba Backend desde `develop` en esa corrida; corresponde repetir la validación contractual después de integrar la PR de Backend. Las PR #12 de Contracts, #87 de Backend y #20 de Frontend permanecían abiertas y sin merge al redactar esta entrada.
