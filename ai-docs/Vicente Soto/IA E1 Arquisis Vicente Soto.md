# AI Log - Vicente Soto

# E1-14 - Definir schemas del protocolo v2 en repo de contratos

## 1. Comprensión de la tarjeta y del requisito

### Prompt relevante

> "No entiendo lo que hicimos eso si, para que es esta feature? Y que hace? Que parte del enunciado nos la pide?"

La consulta permitió relacionar la tarjeta con RDOC04 y distinguir entre:

- lo que exige el enunciado: mantener schemas de los mensajes en el repositorio de contratos
- decisiones internas del grupo, como utilizar JSON Schema Draft 2020-12 o crear un archivo común reutilizable.

### Prompt relevante

> "Ahhh perfecto, entonces ese schemas/v2/common.schema.json que debe llevar?"

Posteriormente se pidió una explicación detallada.

### Prompt relevante

> "Puedes explicarmelo paso a paso para entenderlo? Y porque cumple?"

A partir de estas consultas se definieron y comprendieron conceptos como:

```text
$defs
$ref
baseEnvelope
cityEnvelope
centralEnvelope
uuid
timestamp
cycleId
cityCode
```

---

## 2. Diseño del schema común

Se creó:

```text
schemas/v2/common.schema.json
```

para mantener las reglas compartidas por los distintos mensajes.

El schema común contiene definiciones reutilizables para:

- UUID
- timestamp
- códigos de ciudad
- `cycleId`
- envelope base
- mensajes emitidos por una ciudad
- mensajes emitidos por la central.

---

## 3. Construcción iterativa de los schemas

Después de crear `common.schema.json`, los contratos fueron implementados uno por uno.

Se implementaron schemas para:

### `status-statement`

Se modelaron:

```text
cycleId
data.energy.generationCapacity
data.energy.consumption
data.energy.generationCost
data.validUntil
```

### `transfer`

Se detectó que el mismo tipo de mensaje puede utilizarse tanto desde la central hacia la ciudad como desde la ciudad hacia la central.

Se modelaron además los casos con:

```text
data.quantity
data.becauseOf
```

### `demand-statement`

Se modelaron:

```text
data.balance.quantity
data.balance.valuePerKwh
```

Se mantuvo `quantity` como número con signo, ya que el protocolo contempla valores positivos y negativos.

### `negotiation-proposal`

Se modelaron:

```text
direction
quantity
pricePerEnergy
```

con:

```text
direction = take | give
```

### `give` y `take`

Se modelaron las confirmaciones de negociación con:

```text
target
energy
pricePerEnergy
```

### `negotiation-report`

Se modelaron:

```text
budgetBalance
energyBalance
```

permitiendo balances negativos cuando corresponda.

### `request`

Se modeló:

```text
data.ask
```

sin restringirlo a un enum cerrado, ya que el enunciado muestra ejemplos pero no una lista exhaustiva.

### `ack`

Se modeló:

```text
data.target
```

como referencia al `msgId` del mensaje respondido.

### `nack`

Se modelaron:

```text
reason
code
data.target
data.message
data.cycleId
```

y se validaron las combinaciones `reason`/`code` definidas por el protocolo.

### `error`

Se modelaron los errores:

```text
CYCLE_UNKNOWN
CYCLE_EXPIRED
PRICE_ABOVE_CAP
OVER_CAPACITY
```

incluyendo campos específicos como:

```text
data.cap
data.spare
```

cuando corresponden.

### `distance-table`

Se modeló la tabla dinámica de destinos con:

```text
distance
transportCost
enabled
```

---

## 4. Validación de los contratos

Se utilizó `check-jsonschema` para comprobar que los ejemplos cumplieran los contratos.

Inicialmente se intentó instalar la herramienta directamente mediante:

```bash
python3 -m pip install --user check-jsonschema
```

pero Ubuntu respondió:

```text
error: externally-managed-environment
```

A partir de este problema se utilizó IA para determinar una forma segura de instalar la dependencia sin modificar el Python administrado por el sistema.

La solución aplicada fue crear un entorno virtual:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install check-jsonschema
```

El directorio:

```text
.venv/
```

se agregó a `.gitignore`.

Antes de validar los contratos también se utilizó:

```bash
python3 -m json.tool archivo.json
```

para comprobar la sintaxis JSON.

---

## 5. Resolución de `$ref`

Durante la primera validación de `status-statement` apareció el error:

```text
Failure resolving $ref within schema

Unresolvable: common.schema.json#/$defs/centralEnvelope
```

El problema estaba relacionado con la resolución relativa de `common.schema.json` debido al uso de `$id`.

Se revisó el comportamiento de las referencias y se decidió eliminar los `$id` relativos que estaban cambiando la base utilizada para resolver el archivo.

Después del cambio se ejecutó nuevamente:

```bash
check-jsonschema \
  --schemafile schemas/v2/status-statement.schema.json \
  examples/v2/valid/status-statement.json
```

obteniendo:

```text
ok -- validation done
```

---

## 6. Pruebas positivas y negativas

No se validaron únicamente ejemplos correctos.

También se realizaron pruebas negativas para verificar que las restricciones realmente fueran aplicadas.

### Campo obligatorio

Se creó temporalmente una versión de `status-statement` sin:

```text
cycleId
```

El resultado fue:

```text
'cycleId' is a required property
```

### Demand statement con ambos signos

Se validaron por separado:

```text
demand-statement.json
demand-statement-negative.json
```

para comprobar que cantidades positivas y negativas fueran aceptadas.

### Propuestas take y give

Se validaron independientemente:

```text
negotiation-proposal-take.json
negotiation-proposal-give.json
```

### Transferencias en ambas direcciones

Se validaron:

```text
transfer.json
transfer-payment.json
```

para cubrir tanto transferencias emitidas por la central como pagos realizados por la ciudad.

---

## 7. Ambigüedades detectadas en el enunciado

Durante la definición de los contratos se detectaron dos inconsistencias que fueron discutidas con ayuda de ChatGPT y posteriormente documentadas en el README.

### `distance-table`

La regla general indica que los mensajes emitidos por central utilizan:

```json
"sender": "central"
```

y no `cityId`.

Sin embargo, el ejemplo concreto de `distance-table` utiliza:

```json
"cityId": "COR"
```

Para no hacer que una de las dos formas descritas por el enunciado fuera inválida, el schema acepta ambas representaciones.

### `PRICE_ABOVE_CAP`

La sección general de errores define:

```text
PRICE_ABOVE_CAP → error / 422
```

pero posteriormente un ejemplo muestra:

```json
"type": "nack"
```

Se decidió seguir la definición general del protocolo y modelar `PRICE_ABOVE_CAP` dentro de `error`.

---

## 8. Documentación

Una vez finalizados los schemas se utilizó ChatGPT como apoyo para estructurar la documentación del repositorio.

### Prompt relevante

> "Ok, quiero que me ayudes a documentar en el readme con detalle lo que hicimos, siguiendo lo del enunciado, referenciando archivos específicos de schemas y ejemplos, como poder ejecutarlos y como lo validamos con el ambiente virtual, etc. Al final, que todo quede documentado."

La respuesta se utilizó como base inicial, pero el README fue posteriormente simplificado y editado manualmente.

# E1-15 y E1-16 - Adaptar parser al envelope v2 e Implementar validación de mensajes entrantes

## 1. Comprensión de la tarjeta y planificación

Luego de finalizar E1-14 se utilizó ChatGPT para determinar cuál era la siguiente tarjeta a implementar y cómo se relacionaba con los contratos recién definidos.

### Prompt relevante

> "Ok, con la 14 cerrada, con que deberíamos seguir?"

La consulta permitió identificar E1-15 como el siguiente paso natural, ya que depende de E1-14 y requiere adaptar la recepción de mensajes al envelope v2 definido previamente. También se consultó cómo debía organizarse el trabajo entre los repositorios.

### Prompt relevante

> "Ok, pero partamos de la base, hago una nueva rama con cherry pick del 14 en contracts e implemento ahí?"

A partir de esta consulta se aclaró que E1-15 no debía implementarse en el repositorio de contratos, sino en el repositorio backend, ya que corresponde a comportamiento ejecutable del sistema y no a la definición de contratos.

---

## 2. Análisis de la arquitectura existente

Antes de modificar código se entregó a ChatGPT el contexto de la implementación existente para identificar qué parte del sistema debía modificarse.

### Prompt relevante

> "Ok, antes de nada, te paso contexto de el repo con fastapi y python que estoy utilizando, para que me digas donde debería atacar y con que seguimos."

Se describió la arquitectura actual:

```text
RabbitMQ
   ↓
connector
   ↓
POST /internal/events
   ↓
master
   ↓
PostgreSQL
```

Posteriormente se compartieron los archivos principales involucrados:

```text
connector/connector.py
master/app/schemas.py
master/app/main.py
```

El análisis mostró que `connector.py` actualmente realiza:

```python
payload = json.loads(message.body.decode("utf-8"))
```

y luego reenvía el diccionario recibido al master sin interpretar la estructura específica de E0.

Por otro lado, el formato E0 estaba explícitamente representado en `master/app/schemas.py` mediante:

```text
EventPayload
PackageBodyPayload
DemandPayload
```

con una dependencia de:

```text
type = demand-set
packageBody
```

Esto permitió identificar la diferencia entre el transporte del mensaje y el parsing del protocolo.

---

## 3. Definición del alcance de E1-15

También se discutió si E1-15 y E1-16 podían desarrollarse en una misma rama.

### Prompt relevante

> "Pero podemos hacer las dos tarjetas no? En esta misma rama"

Se decidió trabajar E1-15 y E1-16 dentro de la misma rama debido a su relación directa, pero manteniendo:

- implementación separada
- tests separados
- commits separados
- trazabilidad individual con las tarjetas JIRA.

Después de crear la rama se consultó cómo implementar ambas funcionalidades explícitamente.

### Prompt relevante

> "Ok, ya hice el cambio en la rama, ahora, como hacemos explicitamente las features"

Para E1-15 se definió como responsabilidad exclusiva:

```text
JSON parseado
      ↓
parse_envelope()
      ↓
ProtocolEnvelope
```

sin implementar todavía:

```text
ACK
NACK
errores de negocio
ledger
ciclos
persistencia E1
```

---

## 4. Modelo interno del envelope v2

Se creó una nueva capa de protocolo dentro del connector:

```text
connector/
└── protocol/
    ├── __init__.py
    ├── models.py
    ├── parser.py
    └── tests/
        └── test_parser.py
```

En `models.py` se creó:

```python
ProtocolEnvelope
```

como representación interna de los mensajes v2.

El modelo contiene:

```text
idpk
msg_id
type
timestamp
data
city_id
sender
cycle_id
raw
```

Se utilizaron nombres `snake_case` internamente en Python:

```text
msg_id
city_id
cycle_id
```

manteniendo la correspondencia con el contrato externo:

```text
msgId
cityId
cycleId
```

También se conserva el payload original mediante:

```python
raw
```

para permitir acceso posterior a propiedades específicas de algunos mensajes, como `reason` o `code`.

---

## 5. Implementación del parser

En:

```text
connector/protocol/parser.py
```

se implementó:

```python
parse_envelope(payload)
```

La función transforma un diccionario recibido desde JSON en un `ProtocolEnvelope`.

El parser:

- comprueba la presencia de los campos mínimos del envelope
- transforma `idpk` y `msgId` a objetos `UUID`
- transforma `timestamp` a `datetime`
- reconoce `data`
- obtiene opcionalmente `cityId`, `sender` y `cycleId`
- conserva el payload original.

Se definieron además:

```python
EnvelopeParseError
MissingMsgIdError
```

El caso sin `msgId` se mantiene separado porque el protocolo establece un tratamiento distinto para estos mensajes en las tarjetas posteriores.

En esta etapa no se valida todavía si:

```text
idpk == msgId
type es conocido
sender/cityId son correctos
contenido específico es válido
```

Estas reglas se dejaron para E1-16.

---

## 6. Pruebas del parser

Luego de implementar el parser se crearon pruebas en:

```text
connector/protocol/tests/test_parser.py
```

### Prompt relevante

> "He hecho esto hasta ahora, como lo pruebo?"

A partir de esta consulta se configuró `pytest` dentro de un entorno virtual de Python.

Se utilizó:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install pytest
```

y posteriormente:

```bash
python -m pytest protocol/tests/test_parser.py -v
```

Se implementaron dos casos principales:

### Mensaje emitido por la central

```python
test_parse_v2_envelope()
```

Comprueba un `status-statement` con:

```text
sender = central
cycleId
data
```

y verifica que sus propiedades sean parseadas correctamente.

### Mensaje emitido por una ciudad

```python
test_parse_city_envelope()
```

Comprueba un mensaje `request` con:

```text
cityId = COR
```

y verifica que:

```text
city_id = COR
sender = None
```

El resultado obtenido fue:

```text
collected 2 items

test_parse_v2_envelope PASSED
test_parse_city_envelope PASSED

2 passed
```

---

## 7. Cierre de la tarjeta

Una vez que los tests del parser estuvieron funcionando se consultó el cierre de la tarjeta.

### Prompt relevante

> "Ok, todo listo con lo que dijiste, ahora me salgo del ambiente virtual y hago commit del e-15?"

Se salió del entorno virtual con:

```bash
deactivate
```

y se revisaron los archivos modificados antes del commit.

E1-15 se dejó aislada en un commit propio:

```text
feat: add protocol v2 envelope parser
```

Esto permite mantener trazabilidad entre el historial Git y la tarjeta JIRA aunque E1-15 y E1-16 se desarrollen dentro de una misma rama.

Posteriormente se confirmó el paso a la siguiente tarjeta.

### Prompt relevante

> "Ok, aun no hago push de la rama, ahora con eso commiteado, hay que ir a la implementación de la tarjeta 16 no?"

Con esto se cerró E1-15 y se continuó con E1-16 sin hacer todavía push de la rama.

---

# E1-17 Implementar descarte de no-parseables o sin msgId

---

## Consulta 1 — Comportamiento de mensajes que no se pueden interpretar

### Prompt — resumen

> Quiero seguir con E1-17. Según el enunciado, ¿qué debemos hacer con mensajes que no se pueden parsear o que no tienen `msgId`? ¿Corresponde responderles con NACK?

### Respuesta de ChatGPT

Se revisó la diferencia entre mensajes descartables y mensajes inválidos pero parseables.

Se determinó que:

- JSON inválido debe descartarse.
- UTF-8 inválido debe descartarse.
- Un JSON que no representa un objeto debe descartarse.
- Un mensaje sin `msgId` debe descartarse.
- Estos casos no deben generar ACK ni NACK del protocolo, ya que no existe información suficiente para construir una respuesta válida, especialmente `data.target`.

Se recomendó separar esta etapa de intake de la validación semántica posterior.

---

## Consulta 2 — Separar descarte de validación

### Prompt — resumen 

> ¿Conviene que esto quede separado del parser y de la validación que ya hicimos en E1-15 y E1-16?

### Respuesta de ChatGPT

Sí. Se propuso mantener responsabilidades separadas:

```text
RabbitMQ
   ↓
intake
   ↓
parse
   ↓
validate
   ↓
handler
```

`intake` decide si el mensaje puede siquiera entrar al protocolo.

El resultado permite distinguir:

- mensaje aceptable para seguir procesando;
- mensaje descartado sin respuesta;
- mensaje parseable que posteriormente puede producir NACK.

---

## Consulta 3 — Tests del descarte

### Prompt — resumen 

> ¿Qué casos deberíamos probar para considerar cerrada esta tarjeta?

### Respuesta de ChatGPT

Se agregaron pruebas para comprobar:

- JSON válido con `msgId`;
- JSON inválido;
- UTF-8 inválido;
- JSON sin `msgId`;
- JSON cuyo valor raíz no es un objeto;
- integración con el handler para que un mensaje sin `msgId` termine descartado.

---

# E1-18 Implementar ACK

## Consulta 1 — Construcción del ACK

### Prompt — resumen 

> Ahora quiero seguir con E1-18. ¿Cómo debería construirse el ACK según el protocolo?

### Respuesta de ChatGPT

Se revisó el contrato del protocolo y se definió que un ACK debe:

- tener `type: "ack"`;
- generar un `msgId` nuevo;
- mantener un `idpk` válido y distinto de `msgId`;
- incluir timestamp;
- identificar a la ciudad emisora;
- incluir en `data.target` el `msgId` del mensaje recibido.

Se remarcó que el ACK confirma recepción, no aceptación semántica de una operación.

---

## Consulta 2 — Evitar loops de ACK

### Prompt — resumen 

> ¿Tenemos que responder también los mensajes ACK, NACK o error?

### Respuesta de ChatGPT

No.

Se definió explícitamente que no deben generarse ACKs de:

```text
ack
nack
error
```

Esto evita loops de respuestas entre servicios.

La lógica quedó encapsulada para que el handler decida si corresponde generar una respuesta de protocolo.

---

## Consulta 3 — Publicación de la respuesta

### Prompt — resumen 

> Una vez construido el ACK, ¿cómo lo conectamos con el publisher?

### Respuesta de ChatGPT

Se separó la construcción de la respuesta de su publicación.

El flujo quedó conceptualmente:

```text
mensaje válido
    ↓
handler
    ↓
plan ACK
    ↓
dispatch
    ↓
publisher
    ↓
RabbitMQ
```

El publisher construye el mensaje AMQP e incluye la identificación requerida para publicaciones de la ciudad.

Se agregaron tests tanto para la creación del ACK como para el dispatch y el publisher.

---

# E1-19 Implementar NACK y catálogo de razones

## Consulta 1 — Diferencia entre descarte y NACK

### Prompt — resumen 

> Para E1-19, ¿en qué casos corresponde NACK y en cuáles seguimos descartando como en E1-17?

### Respuesta de ChatGPT

Se distinguieron dos niveles:

```text
No parseable / sin msgId
→ descartar sin responder

Parseable pero inválido
→ generar NACK
```

Esto mantiene consistente E1-17 con E1-19.

---

## Consulta 2 — Catálogo de razones

### Prompt — resumen 

> ¿Qué razones de NACK tenemos que soportar y qué código corresponde a cada una?

### Respuesta de ChatGPT

Se utilizó el catálogo definido por el protocolo:

```text
MALFORMED_MESSAGE     422
UNKNOWN_TYPE          400
IDPK_EQUALS_MSGID     422
IDENTITY_MISMATCH     403
```

El `reason` y `code` viajan en el nivel superior del mensaje.

Dentro de `data` se incluye:

- `target`: `msgId` del mensaje rechazado;
- `message`: descripción legible;
- información adicional del mensaje cuando corresponde.

---

## Consulta 3 — Clasificación automática desde validación

### Prompt — resumen 

> ¿Cómo conectamos los errores que ya detecta validation.py con el NACK correcto sin duplicar lógica?

### Respuesta de ChatGPT

Se recomendó reutilizar el resultado estructurado de la validación.

El handler transforma el resultado en un plan:

```text
válido
→ ACK

unknown type
→ NACK UNKNOWN_TYPE

campo requerido inválido
→ NACK MALFORMED_MESSAGE

idpk == msgId
→ NACK IDPK_EQUALS_MSGID

sin msgId / no parseable
→ discard
```

La construcción del NACK quedó separada de la clasificación para mantener testeables ambas responsabilidades.

---

## Consulta 4 — Propiedades del NACK

### Prompt — resumen 

> ¿El NACK reutiliza el msgId del mensaje original o genera uno nuevo? ¿Qué pasa con cycleId?

### Respuesta de ChatGPT

El NACK debe generar un `msgId` nuevo.

El mensaje original se referencia mediante:

```json
{
  "data": {
    "target": "<msgId original>"
  }
}
```

Si el mensaje rechazado incluye `cycleId`, este se conserva en la respuesta para permitir correlacionarlo.

Se agregaron tests para:

- construcción de NACK;
- conservación de `cycleId`;
- generación de un `msgId` nuevo;
- correspondencia entre `reason` y `code`.

---

# E1-20 Implementar manejo de mensajes `error`  

## Consulta 1 — Separación entre NACK y `error`

### Prompt — resumen 

> Ahora quiero implementar E1-20. ¿Cómo deberían manejarse los mensajes `error` y en qué se diferencian de los NACK que acabamos de implementar?

### Respuesta de ChatGPT

Se distinguió entre errores de formato/protocolo y errores semánticos de negocio.

Un NACK indica que el mensaje recibido no puede aceptarse como mensaje válido del protocolo.

Un mensaje `error`, en cambio, significa que el mensaje fue recibido y entendido correctamente, pero la operación solicitada no puede ejecutarse.

Se trabajó con los cuatro errores definidos por el protocolo:

```text
CYCLE_UNKNOWN       404
CYCLE_EXPIRED       410
PRICE_ABOVE_CAP     422
OVER_CAPACITY       409
```

Además:

- `PRICE_ABOVE_CAP` debe incluir `data.cap`.
- `OVER_CAPACITY` debe incluir `data.spare`.
- `data.target` referencia el `msgId` de la operación original.

---

## Consulta 2 — Persistencia de errores

### Prompt — resumen 

> Quiero que estos errores queden persistidos y que puedan asociarse al mensaje que causó el problema. ¿Cómo lo modelamos?

### Respuesta de ChatGPT

Se propuso una tabla dedicada `ProtocolError`, separada de los eventos de E0 y de las entradas del ledger.

El modelo almacena:

- `idpk`;
- `msg_id`;
- `cycle_id`;
- `reason`;
- `code`;
- `target_msg_id`;
- `message`;
- `cap`;
- `spare`;
- payload original;
- timestamp del protocolo;
- timestamp de recepción.

`target_msg_id` conserva la referencia a `data.target`.

No se agregó una foreign key hacia una tabla general de mensajes porque esa tabla todavía no existe como fuente única para todos los mensajes v2.

---

## Consulta 3 — Idempotencia de errores

### Prompt — resumen 

> ¿Para evitar duplicados debería hacer único `idpk` o `msgId`?

### Respuesta de ChatGPT

Se recomendó utilizar `msgId` como identificador único del mensaje recibido.

La razón es que `idpk` representa la operación lógica y puede mantenerse entre reintentos, mientras que `msgId` identifica una entrega concreta del protocolo.

El endpoint quedó idempotente:

```text
primer POST con msgId X
→ crea ProtocolError

segundo POST con msgId X
→ devuelve el registro existente
```

También se contempló una posible carrera mediante manejo de `IntegrityError`.

---

## Consulta 4 — Endpoint específico para errores

### Prompt — resumen 

> El connector actualmente envía los mensajes al endpoint de E0. ¿Conviene mandar los `error` a otro endpoint?

### Respuesta de ChatGPT

Sí.

Se agregó:

```text
POST /internal/protocol/errors
```

y una variable:

```env
MASTER_ERROR_URL=http://master:8000/internal/protocol/errors
```

El connector selecciona el endpoint según el tipo:

```text
type == "error"
→ MASTER_ERROR_URL

otro tipo
→ MASTER_URL
```

Esto permite incorporar E1-20 sin alterar innecesariamente el endpoint general existente.

---

## Consulta 5 — ACK de protocolo versus ACK de RabbitMQ

### Prompt — resumen 

> Pero si recibimos un `error`, ¿no deberíamos responderle con ACK? En el connector igual hacemos `message.ack()`.

### Respuesta de ChatGPT

Se distinguieron dos conceptos distintos.

El protocolo especifica que no se generan ACKs de mensajes `error`.

En cambio:

```python
message.ack()
```

corresponde al ACK de transporte AMQP, utilizado para indicarle a RabbitMQ que el mensaje fue procesado correctamente.

Por lo tanto:

```text
ACK del protocolo
≠
ACK de transporte RabbitMQ
```

Un `error` no produce un nuevo mensaje `type="ack"`, pero sí puede confirmarse ante RabbitMQ después de haber sido procesado correctamente.

---

## Consulta 6 — Tests del master después del merge del ledger

### Prompt — resumen 

> Los tests del ledger usan unittest y los tests que hicimos para errores están en pytest. ¿Conviene agregar pytest al master o adaptar nuestros tests?

### Respuesta de ChatGPT

Se recomendó no introducir un segundo framework de testing innecesariamente dentro de `master`.

Los 9 tests de `ProtocolError` se migraron a `unittest`.

De esta forma, el master quedó con:

```text
2 tests del ledger
9 tests de ProtocolError
------------------------
11 tests
```

El connector se mantuvo utilizando pytest, ya que ese era el framework establecido allí.

No se convirtió el conjunto completo del connector porque habría agregado riesgo y trabajo fuera del alcance de E1-20.

---

## Consulta 7 — Integración después del rebase

### Prompt — resumen 

> Ya mergeamos E1-17, E1-18, E1-19 y también la PR del ledger. Ahora quiero traer esos cambios a mi rama de E1-20 sin perder lo que hicimos. ¿Hago rebase?

### Respuesta de ChatGPT

Sí.

Se hizo rebase de `feat/e1-20-error-handling` sobre `develop`, que ya contenía:

- E1-17;
- E1-18;
- E1-19;
- persistencia y procesamiento del ledger.

Los conflictos principales estuvieron en:

```text
master/app/models.py
master/app/schemas.py
```

Se conservaron los modelos y schemas del ledger y se reincorporaron los elementos de `ProtocolError`.

También se corrigió `.dockerignore`, eliminando `tests/`, ya que el Dockerfile del master ahora necesitaba copiar los tests para ejecutar `unittest`.

---

## Consulta 8 — Mantener el fix en la misma rama

### Prompt

> Yo haría el fix aquí igual por si acaso. En otro commit, quizás podemos comittear ahora lo que ya tenemos y en otro commit hacer el fix no? No se

### Respuesta de ChatGPT

Se recomendó mantener los cambios claramente separados:

1. commit para adaptar los tests de `ProtocolError` a `unittest`;
2. commit independiente para el bug detectado en el ledger.

Esto permitió mantener una historia comprensible aun cuando ambos cambios permanecieran temporalmente en la misma rama de E1-20.

---

## Consulta 9 — Corrección de `CycleSummaryOut`

### Prompt

> Ok, ya hice el commit, debo cambiar algo aquí entonces?
>
> ```python
> class CycleSummaryOut(BaseModel):
>     cycleId: str
>
>     budgetBalance: Decimal
>     energyBalance: Decimal
>
>     lastOperation: str | None
>     lastOperationAt: datetime | None
>
>     reported: bool
> ```

### Respuesta de ChatGPT

Se identificó que `cycles.py` construía `CycleSummaryOut` utilizando:

```python
lastOperationType=cycle.last_operation_type
```

mientras el schema exigía:

```python
lastOperation
```

Se cambió únicamente:

```python
lastOperation: str | None
```

por:

```python
lastOperationType: str | None
```

Posteriormente se reconstruyó el contenedor y `/cycles` volvió a responder correctamente.

---

## Consulta 10 — Imagen nueva pero contenedor antiguo

### Prompt

> ```text
> docker compose build master
> docker compose run --rm master python -m unittest discover -s tests -v
> ```
>
> Los 11 tests pasan, pero:
>
> ```text
> curl -sS http://127.0.0.1:8001/cycles | python3 -m json.tool
> Expecting value: line 1 column 1 (char 0)
> ```

### Respuesta de ChatGPT

Se explicó que:

```bash
docker compose build master
```

actualiza la imagen, pero no reemplaza automáticamente el contenedor `master` que ya estaba ejecutándose.

Se indicó recrearlo mediante:

```bash
docker compose up -d --build --force-recreate master
```

Después de esperar a que el healthcheck quedara en estado `healthy`, `/cycles` respondió:

```text
HTTP/1.1 200 OK
```

con el campo correcto:

```json
{
  "lastOperationType": null
}
```

---

## Consulta 11 — Validación funcional del ledger después del rebase

### Prompt

> ```bash
> curl -sS http://127.0.0.1:8001/cycles/cycle-e2e-001 | python3 -m json.tool
> ```
>
> ```json
> {
>     "cycleId": "cycle-e2e-001",
>     "finalBudgetBalance": "0.00",
>     "finalEnergyBalance": "60.00",
>     "reconstructedBudgetBalance": "0.00",
>     "reconstructedEnergyBalance": "60.00",
>     "snapshotConsistent": true,
>     "ledger": []
> }
> ```

### Respuesta de ChatGPT

Se utilizó el ciclo de prueba para verificar que los cambios de E1-20 no hubieran roto el ledger integrado.

Se probaron secuencialmente:

```text
status-statement
demand-statement
redelivery de demand-statement
transfer
redelivery de transfer
negotiation-proposal
ack
give
transfer de pago
negotiation-report
```

Se verificó que:

- los balances cambiaran exactamente una vez;
- los redeliveries fueran idempotentes;
- el ledger pudiera reconstruir los balances;
- `snapshotConsistent` permaneciera en `true`;
- una negociación avanzara por:
  - `PROPOSED`;
  - `ACKNOWLEDGED`;
  - `CONFIRMED`;
  - `PAID`;
- el pago quedara asociado mediante `becauseOf`;
- el reporte final coincidiera con los balances reconstruidos.


## Consulta 12 — Actualización de la rama después del rebase

### Prompt

> Que prosigue? Un force with lease para limpiar la historia en mi PR?

### Respuesta de ChatGPT

Sí.

Como la rama había sido rebased sobre `develop`, la historia local y remota habían divergido.

Se recomendó:

```bash
git push --force-with-lease origin feat/e1-20-error-handling
```

en lugar de:

```bash
git push --force
```

para evitar sobrescribir cambios remotos inesperados.

La historia final relevante quedó:

```text
fix: alinear el campo de resumen del ciclo de la última operación
test: migrar las pruebas de error de protocolo a unittest
feat: anadir gestion de errores de protocolo
```

---

# E1-32 y E1-33 - Registro global de `idpk` e idempotencia de reintentos

## Consulta 1 — Problema con la idempotencia existente

### Prompt — resumen

> Queremos implementar E1-32 y E1-33. Actualmente el ledger evita duplicados utilizando `(idpk, operation_type)`, pero ¿eso realmente garantiza que un mismo `idpk` no pueda aplicar un efecto distinto si cambia el payload?

### Respuesta de ChatGPT

Se revisó la implementación existente del ledger y se detectó que la protección:

```text
(idpk, operation_type)
```

no era suficiente como garantía global de idempotencia.

Un caso problemático era el mensaje:

```text
transfer
```

ya que su `operation_type` derivado depende del contenido del payload.

Por ejemplo:

```text
transfer sin becauseOf
→ TRANSFER_IN
```

mientras que:

```text
transfer con becauseOf
→ PAYMENT_RECEIVED
```

Por lo tanto, reutilizar el mismo `idpk` modificando el payload podía producir un `operation_type` distinto y evitar la restricción existente.

Se concluyó que E1-32 debía incorporar un registro global independiente del tipo de efecto aplicado.

---

## Consulta 2 — Diseño de `ProcessedIdpk`

### Prompt — resumen

> ¿Conviene utilizar `InboundMessage` como registro de idempotencia o crear una tabla específica para saber determinísticamente qué `idpk` ya fue procesado?

### Respuesta de ChatGPT

Se recomendó mantener separadas las responsabilidades.

`InboundMessage` representa evidencia de recepciones y puede contener varias filas relacionadas con un mismo `idpk`, por ejemplo:

```text
primera recepción
retry
redelivery
duplicado
```

Por este motivo no se utilizó como fuente para decidir si una operación ya había sido aplicada.

Se creó un nuevo modelo:

```python
ProcessedIdpk
```

con:

```text
idpk
msg_id
message_type
cycle_id
processed_at
```

El campo:

```text
idpk
```

se definió como clave primaria.

De esta manera existe una fuente persistente y determinista para saber si un `idpk` ya produjo exitosamente un efecto de negocio.

---

## Consulta 3 — Claim atómico y concurrencia

### Prompt — resumen

> ¿Cómo hacemos el claim de un `idpk` de forma segura considerando que existen `master` y `master2` y ambos podrían intentar procesar el mismo mensaje?

### Respuesta de ChatGPT

Se descartó una solución basada únicamente en:

```text
SELECT
↓
si no existe
↓
INSERT
```

porque dos procesos concurrentes podrían observar simultáneamente que el registro no existe.

Se implementó el claim mediante PostgreSQL utilizando:

```sql
INSERT ... ON CONFLICT DO NOTHING
```

junto con:

```sql
RETURNING
```

La función:

```python
claim_idpk(...)
```

retorna:

```text
True
```

cuando la fila fue creada y:

```text
False
```

cuando otro procesamiento ya había registrado el mismo `idpk`.

Esto aprovecha la unicidad de la clave primaria en la base de datos para resolver la competencia de forma atómica.

---

## Consulta 4 — Relación entre el claim y la transacción

### Prompt — resumen

> Si registramos el `idpk` antes de ejecutar el efecto, ¿qué pasa si después el procesamiento falla? ¿No quedaría marcado como procesado aunque la operación no se haya aplicado?

### Respuesta de ChatGPT

Se definió que:

```python
claim_idpk(...)
```

no debe realizar `commit`.

El registro de `ProcessedIdpk` forma parte de la misma transacción que el efecto de negocio.

El flujo queda conceptualmente:

```text
claim idpk
    ↓
aplicar efecto
    ↓
actualizar auditoría
    ↓
commit
```

Si ocurre una excepción:

```text
claim idpk
    ↓
procesamiento falla
    ↓
rollback
```

el claim también se revierte.

Esto permite que el mismo mensaje pueda ser procesado nuevamente después de un fallo real.

---

## Consulta 5 — Integración en `/internal/messages`

### Prompt — resumen

> ¿En qué parte del endpoint `/internal/messages` debería hacerse el claim para evitar aplicar otra vez la operación pero mantener igualmente la evidencia de la recepción?

### Respuesta de ChatGPT

Se recomendó mantener primero el registro de la recepción mediante:

```python
record_inbound_message(...)
```

y posteriormente intentar el claim dentro de la transacción de procesamiento.

El flujo quedó:

```text
mensaje recibido
    ↓
crear InboundMessage
    ↓
claim_idpk()
    ↓
┌──────────────────────────────┐
│ idpk nuevo                   │
│ → continuar procesamiento    │
└──────────────────────────────┘

┌──────────────────────────────┐
│ idpk ya procesado            │
│ → DUPLICATE                  │
│ → no ejecutar lógica negocio │
└──────────────────────────────┘
```

Cuando el claim falla, la nueva recepción queda auditada con:

```text
status = DUPLICATE
reason = idpk already processed
```

pero no vuelve a modificar balances, ledger ni otro estado persistente.

---

## Consulta 6 — Migración y datos anteriores

### Prompt — resumen

> Si agregamos ahora `processed_idpks`, ¿qué pasa con operaciones que ya estaban procesadas antes de crear esta tabla?

### Respuesta de ChatGPT

Se agregó una nueva migración Alembic:

```text
4f973b372ca6_add_processed_idpks.py
```

que crea:

```text
processed_idpks
```

junto con los índices correspondientes.

También se incorporó un backfill desde:

```text
inbound_messages
```

considerando registros con:

```text
status = PROCESSED
```

La migración utiliza los mensajes históricos procesados para poblar el nuevo registro y evitar que un retry posterior de una operación antigua vuelva a aplicar su efecto.

---

## Consulta 7 — Pruebas específicas para E1-32 y E1-33

### Prompt relevante

> "Ok, se supone que ya tengo todos cambiados, como pruebo esto? Y además lo que ya hicimos en esta PR de 32 y 33?"

### Respuesta de ChatGPT

Se propuso validar la implementación en distintos niveles.

Primero se creó:

```text
master/tests/test_idempotency.py
```

con un test para E1-32 que comprueba que:

```text
idpk nuevo
→ no aparece procesado

claim
→ exitoso

commit
→ otra sesión puede observarlo

segundo claim
→ rechazado
```

Para E1-33 se agregó un caso deliberadamente adversarial.

El primer mensaje utiliza:

```text
type = transfer
sin becauseOf
```

por lo que produce:

```text
TRANSFER_IN
```

El retry mantiene el mismo `idpk`, pero cambia:

```text
msgId
payload
```

y agrega:

```text
becauseOf
```

Sin la protección global, este segundo mensaje podría transformarse en otro `operation_type`.

Se verificó que el resultado fuera:

```text
DUPLICATE
```

y que:

- el budget cambiara solo una vez;
- el ledger tuviera una sola entrada;
- el mensaje original permaneciera como fuente del efecto;
- existiera un único `ProcessedIdpk`;
- ambas recepciones quedaran registradas en `InboundMessage`.

---

## Consulta 8 — Test de rollback

### Prompt — resumen

> ¿Conviene probar explícitamente que un claim revertido por rollback pueda intentarse nuevamente?

### Respuesta de ChatGPT

Sí.

Se agregó:

```python
test_rolled_back_claim_can_be_retried
```

El test realiza:

```text
claim
↓
rollback
↓
is_idpk_processed() == False
↓
nuevo claim del mismo idpk
↓
True
```

Esto verifica directamente la propiedad transaccional de la implementación.

El resultado es que un fallo durante el procesamiento no consume permanentemente el identificador de idempotencia.

---

## Consulta 9 — Actualización de tests existentes

### Prompt relevante

> "Ok, pero como se donde se debe hacer eso?"

### Respuesta de ChatGPT

Al introducir `ProcessedIdpk`, los tests que utilizan:

```text
POST /internal/messages
```

comenzaron a crear filas adicionales en la nueva tabla.

Para identificar qué tests requerían actualización se utilizó:

```bash
grep -Rni 'internal/messages' master/tests
```

Se identificaron:

```text
test_status_statement.py
test_transfer_received.py
test_distance_table.py
test_idempotency.py
test_demand_statement.py
test_internal_message_audit.py
```

Los `tearDown()` fueron actualizados para limpiar también los registros de:

```text
ProcessedIdpk
```

En mensajes asociados a un ciclo se utilizó:

```python
ProcessedIdpk.cycle_id == self.cycle_id
```

Mientras que `distance-table`, al no utilizar `cycleId`, se limpió mediante los `idpk` generados durante el test.

---

## Consulta 10 — Cambio esperado en auditoría de duplicados

### Prompt — resumen

> Después de agregar la idempotencia global, un test de `status-statement` falla porque esperaba `status-statement already applied` pero ahora recibe `idpk already processed`. ¿Es un error de la implementación?

### Respuesta de ChatGPT

No.

Antes de E1-32 el retry alcanzaba la protección específica de:

```text
status-statement
```

y por eso obtenía:

```text
status-statement already applied
```

Con la nueva protección global, el mismo mensaje es interceptado antes por:

```python
claim_idpk(...)
```

por lo que la razón correcta pasó a ser:

```text
idpk already processed
```

Se actualizó el test para reflejar el nuevo flujo.

---

## Consulta 11 — Validación final de la implementación

### Prompt relevante

> "Ok, se supone que ya tengo todos cambiados, como pruebo esto? Y además lo que ya hicimos en esta PR de 32 y 33?"

### Respuesta de ChatGPT

Se realizó una validación final en varias capas.

Primero:

```bash
python3 -m py_compile ...
git diff --check
```

para comprobar sintaxis y problemas de whitespace.

Posteriormente se reconstruyeron las imágenes:

```bash
docker compose build master migrate
```

y se ejecutó la suite completa:

```bash
docker compose run --rm master \
  python -m unittest discover -s tests -p 'test_*.py' -v
```

El resultado final fue:

```text
Ran 25 tests

OK
```

También se verificó Alembic mediante:

```bash
docker compose run --rm migrate alembic current
docker compose run --rm migrate alembic heads
docker compose run --rm migrate alembic check
```

obteniendo:

```text
4f973b372ca6 (head)
```

tanto para `current` como para `heads`, y:

```text
No new upgrade operations detected.
```

---

## Consulta 12 — Conflicto de Docker durante la verificación final

### Prompt relevante

> "Justo abrí docker desktop, puede ser que eso haya generado los fallos?"

### Respuesta de ChatGPT

Durante la última verificación Alembic apareció un conflicto sobre:

```text
127.0.0.1:5432
```

Se comprobó mediante:

```bash
sudo ss -ltnp | grep 5432
```

que el puerto estaba siendo retenido por:

```text
docker-proxy
```

También se verificó:

```bash
docker context show
docker context ls
sudo systemctl status docker
```

y se determinó que la CLI estaba utilizando el Docker Engine local de WSL:

```text
default
unix:///var/run/docker.sock
```

mientras Docker Desktop había sido abierto paralelamente.

Después de cerrar correctamente Docker Desktop y reiniciar el daemon local, el contenedor:

```text
db
```

volvió a asociarse correctamente al puerto 5432.

Las verificaciones de Alembic y la suite completa se repitieron posteriormente con éxito, confirmando que el incidente correspondía al entorno Docker y no a los cambios de E1-32/E1-33.

# E1-34 Registro consultable de duplicados, descartes y NACK

## Consulta 1 — Alcance de la tarjeta y relación con contratos

### Prompt relevante

> Duda, esto no tiene relación con el repo de contratos? No es necesario tener en consideración eso también?

> Aun no hago lo de OpenAPI, pensaba hacerlo al final final de todo cuando todo esté implementado. En ese sentido, demosle con lo que hay que hacer aquí no?

### Respuesta de ChatGPT

Se revisó el alcance de E1-34 en relación con el repositorio de contratos y con la documentación OpenAPI.

Se concluyó que la tarjeta no requería introducir un nuevo tipo de mensaje RabbitMQ ni modificar los schemas v2 existentes, ya que su objetivo era registrar y consultar eventos ya existentes: duplicados, descartes y NACK.

También se identificó que los nuevos endpoints backend deberían quedar reflejados posteriormente en OpenAPI, pero se decidió mantener esa actualización fuera de esta tarjeta y realizarla al consolidar la API completa.

La implementación se concentró, por lo tanto, en:

- reutilizar la evidencia inbound ya existente;
- registrar descartes y NACK;
- hacer consultables `DUPLICATE`, `DISCARDED` y `NACKED`;
- relacionar duplicados con el mensaje originalmente procesado.

La decisión final de postergar OpenAPI fue tomada por el desarrollador.

## Consulta 2 — Diseño del registro consultable para RF05

### Prompt — resumen

> Revisar cómo implementar E1-34 considerando los modelos y servicios de auditoría que ya existen, evitando crear tablas o migraciones innecesarias.

### Respuesta de ChatGPT

Se revisó la infraestructura existente de auditoría y se observó que `InboundMessage` ya almacenaba la información necesaria para RF05:

- `msg_id`;
- `idpk`;
- `message_type`;
- `cycle_id`;
- payload;
- raw payload;
- estado;
- código y descripción de razón;
- sender;
- momento de recepción y procesamiento.

A partir de esto se recomendó reutilizar `InboundMessage` en vez de crear una nueva tabla.

Se propusieron schemas específicos para auditoría inbound y dos endpoints:

- `POST /internal/audit/inbound`, utilizado internamente para persistir `DISCARDED` y `NACKED`;
- `GET /internal/audit/inbound`, utilizado para consultar `DUPLICATE`, `DISCARDED` y `NACKED`.

La consulta permite filtrar por estado, tipo, razón, `msgId` e `idpk`.

Para duplicados se propuso utilizar `ProcessedIdpk`, incorporado en E1-32/E1-33, para entregar:

- el `msgId` del intento duplicado en `msgId`;
- el `msgId` del procesamiento original en `relatedMsgId`.

También se recomendó que `msgId` e `idpk` fueran strings opcionales en el schema de auditoría, en vez de UUID estrictos, ya que el propósito de RF05 incluye registrar mensajes malformados.


## Consulta 3 — Registro de mensajes descartados por el connector

### Prompt — resumen

> Integrar el registro de mensajes que se descartan antes de entrar al procesamiento normal del protocolo, procurando no perder la evidencia si master o PostgreSQL están temporalmente caídos.

### Respuesta de ChatGPT

Se identificó que los mensajes descartados durante `decode_incoming_payload()` no llegaban al master y, por lo tanto, no podían quedar registrados para RF05.

Se recomendó modificar `DiscardMessage` para conservar el payload parseado cuando este estuviera disponible.

Luego se agregó una función para construir la evidencia de auditoría de un descarte, conservando según disponibilidad:

- `msgId`;
- `idpk`;
- `type`;
- `cycleId`;
- `sender`;
- payload;
- `rawPayload`;
- razón del descarte.

Se incorporó `MASTER_AUDIT_URL` a la configuración del connector y Docker Compose.

El flujo definido fue:

1. intentar decodificar el mensaje;
2. si se produce `DiscardMessage`, construir la evidencia;
3. enviarla a `/internal/audit/inbound`;
4. eliminar el mensaje de RabbitMQ solo si la evidencia fue persistida;
5. reencolarlo si master o la base de datos no están disponibles.

De esta forma, un fallo temporal del backend no provoca la pérdida silenciosa de la evidencia requerida por RF05.

---

## Consulta 4 — Integración de NACK con la auditoría real

### Prompt relevante

> A ver, así está actualmente, donde falla, o que falta?

### Respuesta de ChatGPT

Al revisar el consumer se detectó que la lógica ya implementada en `plan_protocol_response()` y `dispatch_protocol_response()` existía y tenía tests, pero todavía no estaba conectada al loop real de consumo.

Se indicó insertar la planificación de la respuesta después del intake y antes del forwarding normal al master.

Para `handling.action == "nack"` se definió el siguiente flujo:

1. construir la evidencia `NACKED` a partir del mensaje original y del NACK generado;
2. persistir la evidencia en master;
3. si la persistencia falla temporalmente, reencolar el mensaje;
4. si la auditoría queda registrada, publicar el NACK mediante el publisher existente;
5. confirmar finalmente el mensaje original en RabbitMQ.

La evidencia conserva:

- `msgId` e `idpk` del mensaje rechazado;
- tipo y ciclo;
- payload original;
- `reasonCode` proveniente del NACK;
- descripción de la razón.

También se revisó la indentación y estructura del loop para evitar introducir loops de consumo o forwarding duplicados durante la edición.

# E1-21 Identidad AMQP de la ciudad

## Consulta 1 — Validación de consistencia entre `cityId` y `user_id`

### Prompt — resumen

> Revisar cómo reforzar el publisher para asegurar que una publicación de King's Landing lleve `cityId=KLD` en el mensaje y `user_id=city.KLD` como propiedad AMQP.

### Respuesta de ChatGPT

Se propuso fortalecer `build_amqp_message()` para verificar dos condiciones antes de construir el mensaje AMQP:

1. que toda publicación de la ciudad incluya `cityId`;
2. que el `user_id` recibido sea exactamente `city.<cityId>`.

La validación propuesta calcula el valor esperado mediante `build_city_user_id(city_id)` y rechaza localmente la publicación mediante `ValueError` si ambas identidades no coinciden.

Con esto, la consistencia de identidad deja de depender solamente de que el código llamador utilice correctamente ambos campos y pasa a ser una invariante del publisher.

También se propusieron tests para comprobar:

- rechazo de una publicación sin `cityId`;
- rechazo de una publicación con `cityId=KLD` y `user_id` correspondiente a otra ciudad.

---

## Consulta 2 — Validación local de E1-21

### Prompt — resumen

> Ejecutar los tests específicos del publisher y luego toda la suite del connector para comprobar que la nueva validación no rompe el flujo existente.

### Respuesta de ChatGPT

Se recomendó ejecutar:

- compilación de `publisher.py`;
- suite específica de `test_publisher.py`;
- suite completa del connector;
- `git diff --check`.

Los resultados obtenidos fueron:

- `5/5` tests del publisher pasando;
- `51/51` tests del connector pasando;
- `git diff --check` sin errores.

Los tests específicos verifican tanto el caso válido como los casos en que:

- falta `cityId`;
- `cityId` y `user_id` representan ciudades diferentes.

---

## Consulta 3 — Prueba con el broker real

### Prompt — resumen

> Realizar una prueba contra el broker real para comprobar que una publicación con `cityId=KLD` y `user_id=city.KLD` puede enviarse hacia central.

### Respuesta de ChatGPT

Se propuso inicialmente una prueba controlada utilizando las credenciales reales ya configuradas en el `.env` local.

La IA no recibió ni solicitó la contraseña real ni la URL completa con credenciales. Estas permanecieron únicamente en el entorno local del desarrollador.

La prueba utilizó:

- conexión AMQPS;
- exchange `energy.x`;
- routing key `central`;
- `cityId=KLD`;
- propiedad AMQP `user_id=city.KLD`.

RabbitMQ aceptó la publicación correctamente.

Sin embargo, no se recibió una respuesta de central correlacionada con el mensaje dentro de la ventana de espera utilizada, por lo que no se interpretó esta prueba como evidencia de una respuesta explícita de central.

---

## Consulta 4 — Evaluación de pruebas adicionales sobre infraestructura compartida

### Prompt relevante

> Ojo con esto igual cuando probemos cosas.

> Ok, pero entonces, como puedo cerrar la tarjeta?


### Respuesta de ChatGPT

A partir de las restricciones entregadas para el uso de la infraestructura compartida, se recomendó no continuar enviando mensajes artificiales únicamente para intentar obtener una respuesta específica de central.

Se decidió utilizar como evidencia:

- conexión AMQPS real comprobada;
- publicación aceptada por RabbitMQ;
- `cityId=KLD`;
- `user_id=city.KLD`;
- validación local que impide inconsistencias entre ambas identidades;
- suite completa del connector en verde.

Se dejó explícito que no se recibió una respuesta correlacionada de central y, por lo tanto, no se afirmó que central hubiera respondido explícitamente con un resultado particular.

Se prefirió evitar tráfico adicional innecesario sobre el broker compartido.

# Fix E1-18 — Integración del ACK de protocolo

## Consulta 1 — Revisión de tarjetas que dependían de la infraestructura real

### Prompt relevante

> sucede que ahora con las credenciales, no se si lo recuerdad pero estas estaban medias pendientes porque se necesitaba la infra de publicación a la central. En ese sentido, y a partir de esta PR, sería bueno analizar estas PRs nuevamente no?

### Respuesta de ChatGPT

Se revisó nuevamente el estado de E1-17 a E1-20 ahora que la infraestructura RabbitMQ real estaba disponible.

A partir del código actual se identificó que:

- E1-17 ya estaba integrada al flujo real del connector.
- E1-19 ya publicaba NACK desde `consume_forever()`.
- E1-20 tenía un endpoint específico para persistir errores.
- E1-18 tenía un gap de integración: el ACK se construía correctamente mediante `plan_protocol_response()`, pero no se publicaba desde el flujo real de `consume_forever()`.

Se distinguió además entre el ACK de protocolo y `message.ack()` de RabbitMQ, que solamente confirma el delivery de transporte.

---

## Consulta 2 — Revisión del endpoint utilizado por el connector

### Prompt — resumen

> Revisar si el connector debía continuar utilizando `/internal/events` o si los mensajes E1 debían enviarse al nuevo flujo `/internal/messages`.

### Respuesta de ChatGPT

Se revisaron los endpoints existentes en `master`.

Se determinó que:

- `/internal/events` correspondía a un flujo específico anterior basado en `EventPayload`.
- `/internal/messages` era el endpoint general para mensajes E1 ya validados por el connector.
- Este último procesaba tipos como `status-statement`, `transfer`, `demand-statement`, `distance-table`, `negotiation-proposal`, `ack`, `give`, `take` y `negotiation-report`.

Se recomendó actualizar `MASTER_URL` para utilizar:

```text
http://master:8000/internal/messages
```

manteniendo los endpoints especializados para errores y auditoría.

---

## Consulta 3 — Integración del ACK al flujo real

### Prompt relevante

> Quedó bien?

Se mostró el código actualizado de `connector.py`.

### Respuesta de ChatGPT

Se propuso que el flujo exitoso siguiera este orden:

1. validar el mensaje;
2. enviarlo a `master`;
3. esperar una respuesta exitosa;
4. publicar el ACK de protocolo hacia central;
5. confirmar recién entonces el delivery original de RabbitMQ.

Se extrajo esta lógica a una función auxiliar `complete_successful_message()` para hacer explícita y testeable la relación entre:

- publicación del ACK de protocolo;
- `message.ack()` de RabbitMQ.

También se mantuvo el comportamiento de no responder ACK a mensajes `ack`, `nack` o `error`.

---

## Consulta 4 — Tests específicos del nuevo flujo

### Prompt relevante

> Parece que pasaron, pero como podemos estar seguros que están considerando los cambios que hicimos?

### Respuesta de ChatGPT

Se observó que los tests existentes verificaban por separado:

- construcción del ACK;
- dispatch;
- publisher;
- routing.

Sin embargo, no protegían directamente el wiring agregado dentro del connector.

Se recomendó agregar `connector/tests/test_connector_flow.py` para comprobar:

- que el ACK de protocolo se publique antes del ACK de RabbitMQ;
- que un mensaje que no requiere respuesta solo confirme el delivery;
- que, si falla la publicación del ACK de protocolo, no se confirme el delivery original.

Inicialmente los tests fueron escritos como `async def`, pero pytest no tenía configurado un plugin para ejecutarlos directamente.

Se corrigieron para utilizar funciones de test normales junto con `asyncio.run()`, sin agregar nuevas dependencias.

Resultado:

- `3/3` tests específicos del flujo pasando.
- `54/54` tests del connector pasando.

---

## Consulta 5 — Validación de configuración y Docker

### Prompt — resumen

> Verificar que Docker Compose estuviera utilizando realmente `/internal/messages` y que no existiera una configuración antigua sobreescribiendo el nuevo valor.

### Respuesta de ChatGPT

Se recomendó validar la configuración efectiva mediante Docker Compose.

Se confirmó que el runtime resolvía:

```text
MASTER_URL=http://master:8000/internal/messages
MASTER_ERROR_URL=http://master:8000/internal/protocol/errors
MASTER_AUDIT_URL=http://master:8000/internal/audit/inbound
```

También se verificó:

- `docker compose config --quiet`;
- build correcto del connector;
- `git diff --check` sin errores.

---

## Consulta 6 — Prueba con mensajes reales del broker

### Prompt relevante

> Pero y esa prueba aun no la podemos hacer? O como?

### Respuesta de ChatGPT

Se recomendó evitar publicar mensajes artificiales adicionales hacia central y utilizar en cambio una validación pasiva:

- levantar `db`, `master` y `connector`;
- consumir normalmente desde `city.KLD.q`;
- observar mensajes reales provenientes de la infraestructura;
- revisar el flujo mediante logs.

Durante esta prueba llegaron mensajes reales a la cola y el connector los envió correctamente a:

```text
POST /internal/messages
```

obteniendo respuestas `200 OK`.

---

## Consulta 7 — Detección de doble ACK durante la prueba real

### Prompt relevante

Se compartieron logs que mostraban repetidamente:

```text
Message already processed
```

### Respuesta de ChatGPT

A partir de los logs se identificó un bug de runtime que no había sido detectado por los tests iniciales.

`complete_successful_message()` ya ejecutaba:

```python
await message.ack()
```

pero `consume_forever()` ejecutaba nuevamente otro:

```python
await message.ack()
```

después de llamar al helper.

Esto provocaba que `aio-pika` intentara confirmar dos veces el mismo `IncomingMessage`, generando `Message already processed` y reconexiones/redelivery.

Se recomendó eliminar el segundo ACK del call-site y mantener un único punto de confirmación dentro de `complete_successful_message()`.

---

# AI Log - Vicente Soto

# E1-56 - Endpoint de anomalías y auditoría

## 1. Comprensión de la tarjeta y diseño del endpoint

### Prompt relevante

> "Pasó, que sigue?"

A partir de la implementación inicial se revisó qué faltaba para considerar completa la tarjeta E1-56 y cómo demostrar el requisito asociado a RF05.

Se decidió mantener separado el endpoint interno ya existente de E1-34:

```text
GET /internal/audit/inbound
```

y agregar un endpoint destinado a ser consumido desde frontend/demo:

```text
GET /audit/anomalies
```

La decisión permitió reutilizar la evidencia de auditoría ya persistida por E1-34 sin exponer directamente el namespace `/internal/*`.

También se definió que la respuesta pública debía incluir la información necesaria para identificar una anomalía:

```text
msgId
idpk
type
cycleId
status
reasonCode
reason
receivedAt
processedAt
relatedMsgId
```

mientras que información interna como:

```text
payload
rawPayload
```

no debía exponerse desde el endpoint público.

---

## 2. Diseño y validación de tests para RF05

### Prompt relevante

> "Me tengo que mover a master? Quedé hasta aquí"

Se utilizó IA como apoyo para definir la forma de ejecutar los tests dentro del contenedor de `master` y para diseñar casos específicos para el nuevo endpoint.

Inicialmente se agregaron dos tests:

```text
DISCARDED
NACKED
```

Estos verifican que las anomalías puedan consultarse desde:

```text
GET /audit/anomalies
```

y que la respuesta pública entregue la evidencia necesaria sin incluir:

```text
payload
rawPayload
```

Posteriormente se revisó el test existente de E1-34 para duplicados.

### Prompt relevante

> "Todo pasando"

Se reutilizó el mismo escenario de `test_duplicate_links_retry_to_original_message` para agregar un tercer test público.

El caso verifica que, si se recibe nuevamente un mensaje con el mismo `idpk` pero con un `msgId` diferente:

```text
status = DUPLICATE
msgId = msgId del retry
relatedMsgId = msgId del mensaje original
```

De esta forma se comprobaron los tres tipos de evidencia relevantes para E1-56:

```text
DISCARDED
NACKED
DUPLICATE
```

Los tests específicos terminaron con:

```text
3 passed
```

---

## 3. Incorporación de pytest al contenedor

### Prompt relevante

> "Yo lo haría requirements la verdad, dime como lo añado."

Después de reconstruir la imagen de `master`, se detectó que el contenedor no tenía `pytest` instalado:

```text
/usr/local/bin/python: No module named pytest
```

Se decidió agregar:

```text
pytest
```

a:

```text
master/requirements.txt
```

para permitir ejecutar los tests de forma reproducible dentro del contenedor, en lugar de instalar la dependencia manualmente cada vez.

Luego se reconstruyó la imagen y se verificaron nuevamente los tests.

La suite completa del backend en ese momento terminó con:

```text
31 passed
```

---

## 5. Validación HTTP del endpoint

Se verificó que la nueva ruta estuviera registrada en OpenAPI mediante:

```bash
curl -s http://localhost:8001/openapi.json \
  | grep -o '"/audit/anomalies"'
```

obteniendo:

```text
"/audit/anomalies"
```

También se realizaron consultas directas al endpoint:

```bash
curl -i "http://localhost:8001/audit/anomalies?limit=5"
```

y:

```bash
curl -i "http://localhost:8001/audit/anomalies?status=NACKED&limit=5"
```

Ambas respondieron correctamente con:

```text
HTTP/1.1 200 OK
```

y una estructura válida:

```json
{
  "total": 0,
  "items": []
}
```

cuando no existían anomalías persistidas en ese momento.

---

## 6. Revisión de aislamiento y limpieza de tests

### Prompt relevante

> "Queda algo pendiente? Como tocamos setup y teardown aquí, deberíamos tocarlos en otro lado? Recuerdo que algo así hicimos para los cycles no?"

Se utilizó IA para revisar si el nuevo test podía dejar datos persistidos que afectaran otras pruebas.

Se inspeccionaron los tests que utilizan:

```text
/internal/messages
```

y se comprobó que ya realizan limpieza de entidades como:

```text
InboundMessage
ProcessedIdpk
Cycle
LedgerEntry
```

según corresponda.

El nuevo `test_public_audit.py` utiliza el mismo patrón de limpieza, eliminando los registros asociados al `cycleId` e `idpk` creados durante las pruebas.

No fue necesario modificar los demás tests.

---

## 7. Validación posterior a cambios en develop

Después de mergear E1-40 a `develop`, se decidió actualizar la rama de E1-56 antes de cerrar la PR.

### Prompt relevante

> "Dada mi PR del e1 56 creo razonable hacer un pull de develop y hacer un rebase en mi PR y volver a probar que todo funciona."

Se realizó:

```bash
git fetch origin
git rebase origin/develop
```

El rebase terminó sin conflictos.

Se decidió volver a reconstruir `master` y ejecutar nuevamente los tests de E1-56 y la suite completa para comprobar que la integración con el `develop` actualizado no alterara el comportamiento del endpoint.

---

# Conexión Frontend-Backend

## 1. Comprensión y definición del alcance

### Prompt relevante

> La tarjeta que me toca es hacer la conexión frontend backend, no hay mucha especificación en la tarjeta pero es eso, ¿qué necesitas?

A partir del estado de ambos repositorios se decidió evitar agregar funcionalidades de negocio o una arquitectura frontend innecesariamente grande.

El objetivo mínimo definido fue poder demostrar el siguiente flujo:

`React -> GET /health -> FastAPI -> PostgreSQL`

Además, se decidió que la URL del backend no debía quedar hardcodeada dentro de los componentes React.

## 2. Revisión del enunciado

### Prompt relevante

> De la pregunta que me hiciste, no sé si el enunciado dice algo al respecto.

Se revisó el enunciado de la E1 y se identificaron los requisitos relacionados con la integración:

- Backend y frontend deben estar separados.
- El frontend debe ser una SPA.
- Ambos deben servirse mediante HTTPS en producción.
- La API debe quedar detrás de API Gateway.
- CORS debe estar correctamente configurado.
- El frontend será desplegado mediante S3 y CloudFront.

A partir de esto se decidió permitir una configuración local independiente de la configuración de producción.

## 3. Decisiones tomadas

### Backend

Se decidió agregar una variable:

`CORS_ALLOWED_ORIGINS`

Esta variable permite configurar uno o más orígenes autorizados sin dejar `allow_origins=["*"]`.

Para desarrollo local se utilizó:

`http://localhost:5173`

La variable se agregó a:

- `.env.example`
- `.env` local
- `master`
- `master2`

dentro de Docker Compose.

No se agregó al `cycle-scheduler`, ya que este worker no expone una API HTTP.

FastAPI fue configurado mediante `CORSMiddleware`.

### Frontend

Se decidió agregar:

`VITE_API_URL`

Para desarrollo local:

`http://localhost:8001`

Se creó `src/services/api.js` para centralizar las llamadas HTTP al backend, evitando realizar `fetch` directamente contra URLs hardcodeadas desde los componentes.

Se implementaron:

- `apiFetch(path, options)`
- `getHealth()`

`App.jsx` fue modificado para realizar una petición real a `GET /health` y mostrar:

- estado de conexión;
- status del backend;
- instancia que respondió;
- error de conexión en caso de falla.

## 4. Cambios realizados

### Backend

Archivos modificados:

- `.env.example`
- `docker-compose.yml`
- `master/app/config.py`
- `master/app/main.py`

Se agregó parsing de `CORS_ALLOWED_ORIGINS` desde variables de entorno y se configuró `CORSMiddleware`.

### Frontend

Archivos modificados o creados:

- `.gitignore`
- `.env.example`
- `src/App.jsx`
- `src/services/api.js`

El `.env` local se dejó explícitamente fuera de Git.

## 5. Validación de CORS

Primero se comprobó el preflight desde el origen del frontend:

`Origin: http://localhost:5173`

La API respondió correctamente e incluyó:

`access-control-allow-origin: http://localhost:5173`

También se realizó un `GET /health` indicando el mismo origen y se obtuvo:

`{"status":"ok","instance":"master"}`

con el header CORS esperado.

Luego se probó un origen no autorizado:

`Origin: http://evil.example`

El backend respondió al request HTTP, pero no incluyó:

`access-control-allow-origin: http://evil.example`

confirmando que el origen no estaba autorizado por CORS.

## 6. Validación Frontend-Backend

Se inició el frontend mediante:

`npm run dev`

Desde el navegador se comprobó que React consumía correctamente el backend y mostraba:

- `Backend conectado`
- `Status: ok`
- `Instancia: master`

Esto confirmó la conexión real:

`Browser -> React -> FastAPI -> PostgreSQL`

## 7. Validaciones adicionales

### Frontend

Se ejecutaron:

- `npm run lint`
- `npm run build`

Ambos comandos finalizaron correctamente.

También se verificó que `.env` estuviera ignorado por Git.

### Backend

Se ejecutó `py_compile` sobre los archivos modificados:

- `app/main.py`
- `app/config.py`

Luego se ejecutó la suite completa del backend:

`58 passed`

También se utilizó `git diff --check` para revisar problemas de formato.

## 8. Resultado

La conexión frontend-backend quedó operativa y configurable.

El frontend utiliza `VITE_API_URL` como dirección base de la API y dispone de una capa reutilizable mediante `apiFetch`.

El backend permite configurar explícitamente los orígenes autorizados mediante `CORS_ALLOWED_ORIGINS`.

La integración fue comprobada desde el navegador contra `GET /health`, y tanto las validaciones del frontend como la suite completa del backend finalizaron correctamente.

La misma estructura permite cambiar posteriormente la URL local por la URL de producción sin modificar los componentes que consumen la API.

# E1-50 - Reintentar operación con el mismo `idpk`

## 1. Comprensión de la tarjeta y relación con E1-49

### Prompt — resumen

> Quiero implementar E1-50 después de haber terminado el manejo de timeouts de E1-49. ¿Cómo debería hacerse el retry para mantener el mismo `idpk` sin duplicar la operación?

### Respuesta de ChatGPT

Se revisó el comportamiento exigido por RF03 y AD3 para distinguir entre:

```text
idpk
```

como identificador de la operación lógica, y:

```text
msgId
```

como identificador de un mensaje concreto del protocolo.

Se definió que un retry debía:

- conservar el mismo `idpk` de la negociación;
- generar un `msgId` nuevo;
- reutilizar la misma entidad `Negotiation`;
- actualizar `latest_msg_id` para correlacionar respuestas con el intento vigente;
- volver a publicar una nueva `negotiation-proposal`;
- evitar aplicar nuevamente los efectos de negocio que ya hubieran ocurrido.

También se concluyó que E1-50 debía construirse sobre el estado `TIMEOUT` ya implementado en E1-49, sin reemplazar el mecanismo persistente de deadlines.

---

## 2. Diseño del reintento de una negociación

Se utilizó IA para revisar la máquina de estados existente y determinar cómo reincorporar una negociación expirada al flujo normal.

Se agregó la transición:

```text
TIMEOUT
   ↓
PENDING_PUBLICATION
```

El paso por `PENDING_PUBLICATION` permite reutilizar el comportamiento ya existente del outbox:

```text
PENDING_PUBLICATION
        ↓
RabbitMQ confirma publicación
        ↓
PROPOSED
        ↓
deadline de 30 segundos
```

Cuando una negociación entra nuevamente a:

```text
PENDING_PUBLICATION
```

se elimina temporalmente:

```text
deadline_at
```

ya que los 30 segundos no deben comenzar hasta que el mensaje haya sido publicado realmente.

---

## 3. Creación del retry con el mismo `idpk`

Se incorporó en:

```text
master/app/services/negotiation_proposals.py
```

la función:

```python
enqueue_timed_out_negotiation_retries(...)
```

La función busca negociaciones en:

```text
TIMEOUT
```

utilizando bloqueo:

```sql
FOR UPDATE SKIP LOCKED
```

para evitar que dos workers procesen simultáneamente la misma negociación.

Para cada negociación elegible se genera:

```text
mismo idpk
nuevo msgId
misma dirección
misma cantidad
mismo precio
```

y se crea un nuevo:

```python
OutboundMessage
```

con:

```text
status = PENDING
dispatch_required = True
```

La negociación existente actualiza:

```text
latest_msg_id = nuevo msgId
status = PENDING_PUBLICATION
deadline_at = null
```

No se crea una nueva fila `Negotiation` para representar el retry.

---

## 4. Condiciones para realizar el retry

Se definió que no toda negociación en `TIMEOUT` debe ser reintentada indefinidamente.

Antes de generar el nuevo mensaje se comprueba que:

```text
cycle.scheduler_state == NEGOTIATING
```

y que:

```text
now < cycle.valid_until
```

si el ciclo posee una fecha de expiración.

Por lo tanto, una negociación cuyo ciclo ya terminó permanece en `TIMEOUT` y no genera nuevas publicaciones.

---

## 5. Integración con `cycle-scheduler`

### Prompt — resumen

> ¿Dónde conviene ejecutar el retry si los timeouts ya los procesa el worker `cycle-scheduler`?

### Respuesta de ChatGPT

Se recomendó reutilizar el mismo worker persistente utilizado por E1-49.

El flujo del worker quedó conceptualmente:

```text
procesar ventanas de ciclo
        ↓
generar negotiation-report
        ↓
detectar negociaciones expiradas
        ↓
TIMEOUT
        ↓
generar retries elegibles
        ↓
commit
```

La función de retry se incorporó después de:

```python
process_expired_negotiations(...)
```

y antes del `commit` de la iteración.

También se agregó:

```text
negotiation_retries
```

al logging del worker para poder observar cuándo se generan reintentos.

---

## 6. Protección contra doble aplicación del efecto

Durante la revisión se detectó un caso importante.

Una negociación `give` puede haber recibido una confirmación y haber aplicado ya su efecto energético, pero posteriormente entrar en `TIMEOUT` porque el pago de la central no llegó.

Si esa negociación se reintenta, una nueva confirmación no debe volver a modificar el balance energético.

Para evitarlo se reforzó:

```text
master/app/services/negotiation_confirmations.py
```

Antes de aplicar un nuevo efecto al ledger se busca si la misma negociación ya posee una entrada del tipo:

```text
GIVE_CONFIRMED
```

o:

```text
TAKE_CONFIRMED
```

Si el efecto lógico ya existe:

- no se vuelve a aplicar al ledger;
- se comprueba que energía y precio sean consistentes;
- la negociación puede igualmente actualizar su estado;
- `latest_msg_id` puede avanzar hacia la nueva confirmación.

Esto permite mantener la correlación del intento actual sin aplicar dos veces la operación.

---

## 7. Pruebas específicas del retry

Se creó:

```text
master/tests/test_negotiation_retries.py
```

con pruebas para verificar que:

- una negociación en `TIMEOUT` genera un nuevo retry;
- el retry conserva el mismo `idpk`;
- se genera un `msgId` distinto;
- `latest_msg_id` se actualiza;
- la negociación vuelve a `PENDING_PUBLICATION`;
- `deadline_at` queda vacío hasta la publicación;
- ejecutar nuevamente el procesamiento mientras el retry está pendiente no crea otro mensaje;
- no se genera retry si el ciclo ya expiró.

Los tests específicos inicialmente terminaron con:

```text
3 passed
```

---

## 8. Prueba de idempotencia después del retry

### Prompt — resumen

> Además de probar que se crea el retry, ¿cómo comprobamos realmente que una confirmación posterior no aplique dos veces la energía?

### Respuesta de ChatGPT

Se agregó un caso adicional al flujo de confirmaciones.

El escenario reproduce:

```text
negociación GIVE
    ↓
primera confirmación
    ↓
energía aplicada al ledger
    ↓
timeout esperando pago
    ↓
retry
    ↓
nueva confirmación
```

El test comprueba que después de la segunda confirmación:

```text
solo existe un GIVE_CONFIRMED
```

y que el balance energético permanece con el efecto de una sola operación.

También se comprueba que la negociación queda correlacionada con la confirmación más reciente.

Los tests de:

```text
test_negotiation_confirmations.py
test_negotiation_retries.py
```

terminaron con:

```text
13 passed
```

---

## 9. Validación de regresiones

Después de reconstruir la imagen de `master` se ejecutaron primero los tests relacionados directamente con:

```text
negotiation_state
negotiation_timeouts
negotiation_confirmations
negotiation_payments
negotiation_proposals
```

obteniendo:

```text
45 passed
```

Posteriormente se ejecutó la suite completa:

```bash
docker compose exec master python -m pytest -q
```

con resultado:

```text
122 passed
```

De esta forma se verificó que el mecanismo de retry no rompiera los flujos anteriores de negociación, pagos, timeouts e idempotencia.


# E1-54 - Endpoints de consulta de negociaciones

## 1. Análisis de dependencias y definición del alcance

### Prompt relevante

> "Ok, ahora tengo que hacer este, siento que no es tan dependiente de lo que acabo de hacer ahora no? Asi que podría volver a develop sin haber mergeado esta PR que acabamos de hacer no? E implementar esto"

### Respuesta de ChatGPT

Se revisaron las dependencias de E1-54 y se observó que la tarjeta dependía de:

```text
E1-44
E1-45
E1-46
E1-47
E1-48
E1-49
```

pero no de E1-50.

Por este motivo se decidió crear la nueva feature desde `develop`, manteniendo E1-50 y E1-54 en PRs independientes.

También se identificó que E1-54 era principalmente una funcionalidad de consulta HTTP y no requería modificar:

```text
ledger
scheduler
timeouts
pagos
RabbitMQ
```

---

## 2. Revisión de la API existente

### Prompt relevante

Se compartieron:

```text
master/app/routers/negotiations.py
master/app/schemas.py
```

para determinar qué faltaba.

### Respuesta de ChatGPT

Se observó que el router existente solamente exponía:

```http
POST /negotiations
```

y que ya existía:

```python
NegotiationOut
```

con información sobre:

```text
idpk
direction
requestedQuantity
offeredPrice
status
confirmedEnergy
confirmedPrice
paymentQuantity
deadlineAt
```

A partir de esto se decidió reutilizar el mismo modelo persistido y el mismo schema de salida en vez de crear una nueva representación independiente.

---

## 3. Diseño de los endpoints de consulta

Se propusieron dos endpoints:

```http
GET /negotiations
GET /negotiations/{negotiation_id}
```

El primero permite obtener el historial de negociaciones.

El segundo permite consultar el estado actual de una negociación específica.

Ambos utilizan:

```python
Depends(verify_jwt)
```

manteniendo la misma protección mediante JWT que ya existía para la creación de negociaciones.

Para el detalle se definió además:

```text
404 Negotiation not found
```

cuando el identificador solicitado no existe.

---

## 4. Ampliación de `NegotiationOut`

Para que el frontend dispusiera de suficiente contexto se agregaron a:

```python
NegotiationOut
```

los campos:

```text
cycleId
createdAt
updatedAt
```

Además se creó:

```python
NegotiationsOut
```

con la forma:

```text
total
items
```

para representar la respuesta del listado.

El endpoint:

```http
GET /negotiations
```

ordena las negociaciones mediante:

```python
Negotiation.created_at.desc()
```

de manera que las más recientes aparezcan primero.

---

## 5. Compatibilidad con la consulta de ciclos

### Prompt — resumen

> Ya agregué los nuevos campos al schema y a `routers/negotiations.py`. ¿Con qué seguimos?

### Respuesta de ChatGPT

Se detectó que:

```text
GET /cycles/{cycle_id}
```

también utiliza:

```python
NegotiationOut
```

para construir la lista de negociaciones asociadas a un ciclo.

Por lo tanto, al hacer obligatorios:

```text
cycleId
createdAt
updatedAt
```

también fue necesario actualizar:

```text
master/app/routers/cycles.py
```

La función:

```python
_negotiation_to_out(...)
```

de ese router fue modificada para incluir los nuevos campos.

Esto evitó que la ampliación del schema rompiera un endpoint existente.

---

## 6. Diseño de tests reutilizando la infraestructura existente

### Prompt relevante

> "Llevo esto hasta ahora, con que se sigue?"

Posteriormente se revisó qué archivo de tests debía utilizarse.

Se identificó:

```text
master/tests/test_negotiations.py
```

que ya contenía:

- `TestClient`;
- generación local de JWT;
- mocks de JWKS;
- helpers para crear ciclos;
- limpieza de negociaciones y mensajes outbound.

En vez de crear otro archivo y duplicar esa infraestructura, se decidió extender este conjunto de pruebas.

Se agregó un helper:

```python
_create_negotiation(...)
```

para poder crear directamente negociaciones en distintos estados y comprobar los endpoints de consulta.

---

## 7. Casos de prueba de E1-54

Se agregaron pruebas para:

```text
GET /negotiations
```

verificando:

- respuesta `200`;
- presencia de `total` e `items`;
- exposición de `cycleId`;
- estado de la negociación;
- energía y precio confirmados;
- `createdAt`;
- `updatedAt`.

También se verificó que el listado ordenara las negociaciones desde la más reciente a la más antigua.

Para:

```text
GET /negotiations/{id}
```

se probaron:

- consulta exitosa;
- información completa de una negociación `PAID`;
- `confirmedEnergy`;
- `confirmedPrice`;
- `paymentQuantity`;
- respuesta `404` para un identificador inexistente.

Finalmente se comprobó que ambos endpoints respondieran:

```text
401
```

cuando no se entrega un JWT.

---

## 8. Validación de la implementación

Después de reconstruir el contenedor:

```bash
docker compose up -d --build master
```

se ejecutaron inicialmente los tests específicos:

```bash
docker compose exec master python -m pytest \
  tests/test_negotiations.py \
  -q
```

obteniendo:

```text
13 passed
```

Posteriormente se ejecutó la suite completa:

```bash
docker compose exec master python -m pytest -q
```

con resultado:

```text
123 passed
```

Durante la revisión final se detectó que, aunque existían tests para el `404` y la autenticación del endpoint individual, faltaba un caso explícito para:

```text
GET /negotiations/{id}
→ 200
```

Se agregó ese test con una negociación en estado:

```text
PAID
```

y se verificó que pasara correctamente.

---

## 9. Relación con el frontend

### Prompt relevante

> "El nuevo test está pasando, ahora una duda, en el repo de front debería hacer algo? O aun no?"

### Respuesta de ChatGPT

Se revisaron las tarjetas pendientes del frontend y se concluyó que E1-54 debía mantenerse como una PR exclusivamente backend.

La tarjeta que consume directamente estos endpoints es:

```text
E1-62 - Seguimiento e historial de negociaciones
```

que depende explícitamente de:

```text
E1-54
E1-61
```

La separación definida quedó:

```text
E1-54
Backend:
GET /negotiations
GET /negotiations/{id}

E1-61
Frontend:
formulario para crear negociación

E1-62
Frontend:
seguimiento e historial
```

Por lo tanto, no fue necesario modificar todavía el repositorio frontend como parte de E1-54.

Los endpoints implementados quedan preparados para ser consumidos posteriormente por E1-62.

# E1-35 — Diseñar OpenAPI inicial de la API

## 1. Orientación sobre alcance y ubicación del OpenAPI

### Prompt relevante

> Tengo que hacer las tarjetas E1-35 y E1-36, pero estoy un poco perdido respecto de dónde hacerlo según el enunciado. Se supone que la API ya está lista. El repo de contratos ya existe. ¿Con qué puedo avanzar ahora y con qué no?

### Respuesta de ChatGPT

ChatGPT revisó las dependencias de la tarjeta E1-35 y el requisito RDOC04 del enunciado. A partir de esto indicó que E1-35 podía realizarse inmediatamente, ya que sus dependencias eran:

- E1-04: repositorio organizacional de contratos.
- E1-23: diseño del modelo de persistencia del ledger.

También identificó que la especificación OpenAPI debía quedar versionada en el repositorio organizacional de contratos, específicamente dentro de `docs/openapi/`.

Se aclaró además que E1-35 correspondía a un contrato inicial de la API y que no era necesario esperar a que todas las vistas del frontend estuviesen implementadas. Dado que el backend ya estaba avanzado, se propuso aprovechar la implementación real como fuente de información para construir un contrato consistente.

---

## 2. Uso de Codex para auditar el backend

### Prompt relevante

Se utilizó Codex sobre el repositorio del backend con un prompt preparado con ayuda de ChatGPT. El objetivo era inspeccionar la implementación real sin modificar archivos.

Prompt utilizado, resumido:

> Analiza este repositorio backend de forma exhaustiva para preparar E1-35.
>
> No modifiques ningún archivo.
>
> Revisa routers, schemas/Pydantic, autenticación, `main.py`, configuración, persistencia y tests.
>
> Necesito:
>
> - inventario completo de rutas HTTP;
> - distinguir rutas públicas para frontend de rutas internas;
> - método, path, parámetros, body, response y status codes;
> - modelos exactos de request y response;
> - autenticación JWT;
> - ejemplos obtenidos de tests;
> - diferencias entre `/openapi.json` generado por FastAPI y el contrato público que debería versionarse;
> - inconsistencias o decisiones pendientes;
> - propuesta de estructura para `openapi/openapi.yaml`.
>
> Revisa especialmente ciclos, conectividad, negociaciones, creación de propuestas y anomalías.

### Resultado de Codex

Codex identificó 19 operaciones HTTP de aplicación, pero concluyó que el alcance inicial de E1-35 debía cubrir 7 operaciones sobre 6 paths:

```text
GET  /cycles
GET  /cycles/{cycle_id}
GET  /connectivity
GET  /negotiations
POST /negotiations
GET  /negotiations/{negotiation_id}
GET  /audit/anomalies
```

También identificó que las rutas `/internal/*` correspondían a comunicación entre componentes internos y no debían formar parte del contrato HTTP público destinado al frontend.

El análisis permitió obtener directamente desde el código:

- request y response models;
- campos obligatorios y opcionales;
- campos nullable;
- serialización de valores `Decimal`;
- errores HTTP;
- filtros de `/audit/anomalies`;
- comportamiento idempotente de `POST /negotiations`;
- autenticación JWT;
- ejemplos respaldados por tests;
- diferencias entre la documentación automática de FastAPI y el contrato público deseado.

Codex detectó además que FastAPI generaba un esquema OAuth2 mediante `OAuth2PasswordBearer(tokenUrl="token")`, aunque el backend no implementaba `/token`. Dado que la autenticación real del proyecto utiliza Auth0, esto fue considerado al diseñar el contrato público.

---

## 3. Definición del alcance del contrato público

### Prompt relevante

> Todo esto me entregó Codex. ¿Cómo seguimos con E1-35?

### Respuesta de ChatGPT

ChatGPT analizó el informe y recomendó no copiar directamente el `/openapi.json` generado por FastAPI.

Se decidió construir explícitamente un contrato público porque el OpenAPI automático incluía:

- rutas internas;
- endpoints heredados de E0;
- endpoints operacionales;
- un esquema de autenticación que no representaba correctamente el flujo real con Auth0;
- ausencia de varios errores de negocio;
- ausencia del `200` idempotente de `POST /negotiations`.

Se mantuvo el alcance de siete operaciones y se excluyeron de esta primera versión:

```text
/internal/*
/history
/history/{event_id}
/distance-table
/health
```

La intención fue que `openapi.yaml` representara específicamente la API HTTP utilizada por la SPA.

---

## 4. Verificación previa de la implementación

### Prompt relevante

Antes de congelar el contrato se decidió ejecutar las suites completas del backend y del connector.

### Resultado

Backend:

```text
128 passed in 7.49s
```

Connector:

```text
54 passed in 0.27s
```

Esto permitió utilizar una versión con todos los tests actuales pasando como base para documentar el contrato.

También se confirmó el estado del repositorio de contratos:

```text
Arquisis-G10-Contracts/
├── README.md
└── docs/
    ├── examples/
    ├── openapi/
    │   └── .gitkeep
    └── schemas/
```

La rama utilizada fue:

```text
feat/e1-35-open-api
```

creada directamente desde el `develop` actualizado.

---

## 5. Creación inicial de `openapi.yaml`

### Prompt relevante

> La rama está limpia y `docs/openapi/` solamente tiene `.gitkeep`. ¿Cómo seguimos?

### Respuesta de ChatGPT

Se creó:

```text
docs/openapi/openapi.yaml
```

con:

```yaml
openapi: 3.1.0
```

y metadata para:

```text
EnergyShark E1 API
version: 2.0.0
```

También se definieron los tags:

```text
cycles
connectivity
negotiations
audit
```

Para autenticación se definió:

```yaml
BearerAuth:
  type: http
  scheme: bearer
  bearerFormat: JWT
```

La descripción fue posteriormente ajustada para dejar explícito que el access token JWT es emitido por Auth0 y enviado mediante:

```text
Authorization: Bearer <token>
```

Se optó por no documentar un `tokenUrl`, ya que Auth0 es el proveedor externo y el backend no expone un endpoint `/token`.

---

## 6. Definición de schemas reutilizables

### Prompt relevante

> Ya está creado el archivo base. ¿Cómo seguimos?

### Respuesta de ChatGPT

ChatGPT propuso los componentes reutilizables basándose en los modelos que Codex había extraído directamente del backend.

Se documentaron, entre otros:

```text
DecimalString
DecimalInputPositive
DecimalInputNonNegative
HTTPError
ValidationError
HTTPValidationError

NegotiationCreate
NegotiationOut
NegotiationsOut

LedgerEntryOut
NegotiationReportOut

CycleSummaryOut
CyclesOut
CycleDetailOut

ConnectivityItemOut
ConnectivityOut

AuditAnomalyOut
AuditAnomalyListOut
```

Se respetaron detalles de la implementación, por ejemplo:

- los `Decimal` de salida se serializan como strings;
- `distance` y `transportCost` son números;
- `direction` del request acepta `give` y `take`;
- algunos campos de salida permanecieron como `string` en lugar de forzarlos a `enum`, porque el schema Pydantic real no los restringía;
- los campos nullable se documentaron utilizando sintaxis compatible con OpenAPI 3.1.

---

## 7. Corrección de sintaxis YAML

### Prompt relevante

Al validar el archivo se obtuvo:

```text
yaml.scanner.ScannerError:
mapping values are not allowed here
line 150, column 79
```

### Respuesta de ChatGPT

ChatGPT identificó que una descripción inline contenía `:`:

```yaml
description: Dirección de la negociación. Valores actuales conocidos: give, take.
```

Se reemplazó por un bloque multilínea:

```yaml
description: |
  Dirección de la negociación.
  Valores actuales conocidos: give, take.
```

Después de la corrección se validó correctamente:

```text
OpenAPI: 3.1.0
Schemas: 18
Security schemes: ['BearerAuth']
```

---

## 8. Revisión de coherencia con schemas y ejemplos del protocolo

### Prompt relevante

> ¿Hay que ver si esto conversa correctamente con `docs/schemas` y con los ejemplos de este repo?

### Respuesta de ChatGPT

ChatGPT aclaró que existen dos tipos distintos de contrato:

```text
docs/schemas/v2/
    → mensajes del protocolo RabbitMQ

docs/examples/v2/valid/
    → ejemplos válidos de esos mensajes

docs/openapi/openapi.yaml
    → API HTTP consumida por el frontend
```

Se revisaron específicamente:

```text
negotiation-proposal.schema.json
distance-table.schema.json
status-statement.schema.json
```

junto con sus ejemplos.

Se verificó que las representaciones fueran semánticamente coherentes aunque no fueran idénticas.

Por ejemplo, el protocolo utiliza:

```text
data.quantity
data.pricePerEnergy
```

mientras que la API HTTP recibe:

```text
requestedQuantity
offeredPrice
```

El backend posteriormente construye el mensaje completo del protocolo.

También se comprobó que `/connectivity` transforma el mapa recibido en `distance-table` en una lista de objetos con:

```text
destination
distance
transportCost
enabled
```

Para `statusStatement`, se mantuvo un objeto abierto en OpenAPI porque el backend lo declara como un diccionario genérico, pero se agregó un ejemplo consistente con el contrato del protocolo.

---

## 9. Documentación de ciclos y conectividad

### Respuesta de ChatGPT

Se agregaron inicialmente:

```text
GET /cycles
GET /cycles/{cycle_id}
GET /connectivity
```

incluyendo:

- summaries;
- descriptions;
- `operationId`;
- parámetros de path;
- response schemas;
- errores `404`;
- ejemplos.

Posteriormente se validó:

```text
OpenAPI: 3.1.0
Paths:
- /cycles
- /cycles/{cycle_id}
- /connectivity

Schemas: 18
```

---

## 10. Documentación de negociaciones y Auth0

### Respuesta de ChatGPT

Se agregaron:

```text
GET  /negotiations
POST /negotiations
GET  /negotiations/{negotiation_id}
```

Las tres operaciones se documentaron con:

```yaml
security:
  - BearerAuth: []
```

porque son los endpoints que actualmente requieren JWT.

Para `POST /negotiations` se documentó una particularidad importante de idempotencia:

```text
201 → negociación nueva
200 → idpk ya existente
```

También se documentaron:

```text
401 → token ausente, inválido o expirado
404 → ciclo o negociación inexistente
422 → validación o reglas de negocio
503 → configuración/JWKS de autenticación no disponible
```

Entre los errores de negocio incluidos se encuentran:

```text
PRICE_ABOVE_CAP
OVER_CAPACITY
```

Se dejó explícito además que una respuesta `201` significa que la intención de publicación fue persistida, pero no garantiza que la central haya aceptado todavía la propuesta.

Después de estos cambios se obtuvo:

```text
Paths: 5
Operations: 6
Schemas: 18
```

---

## 11. Documentación de anomalías

### Respuesta de ChatGPT

Finalmente se incorporó:

```text
GET /audit/anomalies
```

con los filtros reales del backend:

```text
status
type
reasonCode
msgId
idpk
limit
```

`status` se restringió a:

```text
DUPLICATE
DISCARDED
NACKED
```

y `limit` quedó documentado entre `1` y `500`, con valor por defecto `100`.

Se añadió además un ejemplo basado en un caso de NACK cubierto por tests.

Con esto se obtuvo:

```text
OpenAPI: 3.1.0
Paths: 6
Operations: 7

GET    /cycles
GET    /cycles/{cycle_id}
GET    /connectivity
GET    /negotiations
POST   /negotiations
GET    /negotiations/{negotiation_id}
GET    /audit/anomalies

Schemas: 18
```

---

## 12. Validación del contrato

### Prompt relevante

> Ya están las siete operaciones. ¿Qué falta antes de cerrar E1-35?

### Respuesta de ChatGPT

Se prepararon scripts de validación para verificar:

- existencia de todos los `$ref`;
- unicidad de los `operationId`;
- existencia del security scheme `BearerAuth`.

Resultado:

```text
Schemas: 18
Security schemes: ['BearerAuth']
Refs: 50
Missing refs: none
Operation IDs:
['listCycles',
 'getCycleDetail',
 'getConnectivity',
 'listNegotiations',
 'createNegotiation',
 'getNegotiation',
 'listAuditAnomalies']

Unique operation IDs: True

Basic contract checks: OK
```

También se compararon las operaciones documentadas contra `/openapi.json` del backend real.

Resultado:

```text
Missing from backend:

All contract operations exist in backend: True
```

Esto permitió verificar que ninguna de las siete operaciones del contrato hubiese sido inventada y que todas existen en la implementación actual.

---

## 13. Actualización del README

### Prompt relevante

> ¿En qué parte añadirías la documentación de OpenAPI al README?

### Respuesta de ChatGPT

Se recomendó agregar una sección `## OpenAPI` inmediatamente después de `## Contenido` y antes de `## Schema común`.

Se actualizó además el árbol del repositorio para incluir:

```text
docs/openapi/
└── openapi.yaml
```

La sección agregada explica:

- ubicación del OpenAPI;
- alcance de la API pública;
- uso de Auth0;
- esquema `Authorization: Bearer <token>`;
- diferencia entre contratos del protocolo y contrato HTTP.

Se dejó explícita la separación:

```text
docs/schemas/    → contratos del protocolo de mensajería
docs/examples/   → ejemplos del protocolo
docs/openapi/    → contrato de la API HTTP frontend/backend
```

---

## 14. Cierre de E1-35

### Resultado

Se consideró cumplido el criterio de cierre de E1-35:

> OpenAPI inicial versionado en el repo de contratos.

Los principales artefactos producidos fueron:

```text
docs/openapi/openapi.yaml
README.md
```

El contrato quedó compuesto por:

```text
6 paths
7 operaciones
18 schemas
Bearer JWT / Auth0
errores HTTP
ejemplos
idempotencia de negociaciones
filtros de anomalías
```

Antes del cierre también se habían verificado:

```text
Backend:   128 tests passed
Connector: 54 tests passed
```

Finalmente se abrió una Pull Request desde:

```text
feat/e1-35-open-api
```

hacia:

```text
develop
```