## Prompt

Adjunté el enunciado de la Entrega 0 (Enunciado_E0_v1.pdf) y las ayudantías, y pedí:

"Hola. Para mi curso de Arquitectura de Sistemas de Software se me pidió hacer para la tarea 1 lo que se menciona en el enunciado. Necesito que me expliques todos los contenidos, aplicaciones y librería que debo manejar para enfrentar la tarea. Primero explícame en general lo que hay que hacer, luego los conceptos generales y luego ve explicándome en qué orden debo realizar los pasos para hacerlo. Asume que no entiendo qué es una cola de eventos, qué es rabbitMQ, AWS EC2, protocolos http/htpps, cloud, docker, nginx, APIs REST, etc. Dame las explicaciones usando python y FastAPI. Por favor y gracias."

## Respuesta

Sí. Revisé el enunciado E0 / EnergyShark y las tres ayudantías que subiste. Voy a explicarlo suponiendo literalmente que partes desde cero en desarrollo web, redes, cloud y sistemas distribuidos.

Hay una idea que conviene entender desde el principio:

La tarea no es “hacer una API con FastAPI”.
La tarea es construir y desplegar un pequeño sistema distribuido, donde FastAPI es solo uno de sus componentes.

El propio enunciado dice que el objetivo es familiarizarse con protocolos, integraciones, seguridad y deployment en cloud.

1. ¿Qué tienes que construir, en términos simples?

Tu sistema va a recibir periódicamente información sobre demanda eléctrica.

Tú no generas esos datos.

Existe un sistema del curso que los genera y los envía mediante RabbitMQ. Tu programa tiene que:

Escuchar RabbitMQ constantemente.
Recibir cada evento.
Convertir el mensaje JSON recibido a datos de Python.
Enviarlo mediante HTTP a tu aplicación FastAPI.
Guardarlo en una base de datos.
Permitir consultar esos datos mediante una API.
Tener todo funcionando en un servidor AWS EC2 accesible desde Internet.

El flujo completo que yo te recomiendo imaginar es:
```
                       SISTEMA DEL CURSO
                              │
                              ▼
                         RabbitMQ
                              │
                       observer.X.q
                              │
                              ▼
                   ┌───────────────────┐
                   │     connector     │
                   │      Python       │
                   │     aio-pika      │
                   └─────────┬─────────┘
                             │
                         HTTP POST
                             │
                             ▼
                   ┌───────────────────┐
                   │      master       │
                   │      FastAPI      │
                   │      Python       │
                   └─────────┬─────────┘
                             │
                             ▼
                   ┌───────────────────┐
                   │    PostgreSQL     │
                   │   base de datos   │
                   └───────────────────┘
```
```
USUARIO
   │
   │ https://tudominio.com/history
   ▼
┌──────────────┐
│    NGINX     │  ← instalado directamente en EC2
└──────┬───────┘
       │
       ▼
    FastAPI
       │
       ▼
  PostgreSQL
```
Esto recoge exactamente la separación exigida por el enunciado: un servicio connector independiente consume RabbitMQ y alimenta a master mediante HTTP POST; master, connector y la base de datos deben poder componerse con Docker Compose.

2. Antes de programar: ¿qué es un “evento”?

Supongamos que ocurre:

Santiago necesita 500 GW de energía.

Un sistema puede representar esa situación como un mensaje:
```
{
    "city": "Santiago",
    "demand": 500,
    "unit": "GW"
}
```
Eso sería información sobre algo que ocurrió.

En arquitecturas de software se suele llamar evento.

En tu tarea los eventos tienen aproximadamente esta estructura:
```
{
    "idpk": "UUIDv4",
    "type": "demand-set",
    "packageBody": {
        "demands": [
            {
                "city": "New New York",
                "demand": 1013.123,
                "unit": "GW"
            }
        ],
        "validUntil": "2026-12-12T00:00:00Z",
        "metaContent": "...",
        "constraints": {}
    }
}
```
Además, cuando tú lo recibes, tienes que registrar un:
```
receivedAt
```
para saber cuándo llegó a tu sistema.

3. ¿Qué es JSON?

JSON es simplemente una forma estándar de representar información como texto.

Esto:
```
evento = {
    "city": "Santiago",
    "demand": 500
}
```
es un diccionario de Python.

Su representación JSON sería:
```
{
    "city": "Santiago",
    "demand": 500
}
```
RabbitMQ te entregará esencialmente texto/binario que contiene JSON.

Por eso tendrás que hacer algo conceptualmente equivalente a:
```
import json

datos = json.loads(mensaje)
```
y obtendrás un diccionario de Python.

El enunciado indica específicamente que los mensajes llegan como un string JSON que debes parsear.

4. ¿Qué es una cola?

Este es probablemente el concepto más importante de la tarea.

Imagínate una fila en un banco:
```
Persona 1
Persona 2
Persona 3
Persona 4
```
La primera que llegó es normalmente la primera que atienden.

Una cola de mensajes hace algo parecido:
```
Evento A
Evento B
Evento C
Evento D
```
Tu programa va sacando mensajes para procesarlos.

RabbitMQ
```
┌─────────────────────────────┐
│ Evento A │ Evento B │ C │ D │
└─────────────────────────────┘
     ↓
 connector
```
Si tu programa tarda un poco, los mensajes pueden permanecer esperando en la cola.

Esto desacopla al sistema que produce información del sistema que la consume.

5. ¿Qué problema resuelve eso?

Sin RabbitMQ tendríamos algo así:
```
Central ───HTTP───> Tu aplicación
```
Si tu aplicación se cae:
```
Central ───X───> Tu aplicación caída
```
podrías perder información.

Con un intermediario:
```
Central
   │
   ▼
RabbitMQ
   │
   ├── Evento 1
   ├── Evento 2
   ├── Evento 3
   │
   ▼
Tu aplicación
```

los sistemas quedan más desacoplados.

La ayudantía precisamente destaca que RabbitMQ permite comunicación asíncrona: los sistemas no tienen que hablar al mismo tiempo ni esperar una respuesta inmediata.

6. Entonces, ¿qué es RabbitMQ?

RabbitMQ es un message broker.

En español:

un intermediario de mensajes.

No es tu aplicación.

No es tu base de datos.

No es una API.

Su trabajo es recibir mensajes y distribuirlos.

Hay cuatro palabras que debes aprender:
```
Producer
   ↓
Exchange
   ↓
Queue
   ↓
Consumer
```

Producer


Es quien genera mensajes.

En tu tarea será la infraestructura del curso.

Exchange

Recibe mensajes y decide hacia qué cola enviarlos.

En tu caso aparece:
```
fulfillment.x
```
Queue

Es donde esperan los mensajes.

A cada alumno se le asigna una:
```
observer.X.q
```
donde X corresponde a tu identificador.
```
Consumer
```
Programa que lee la cola.

Ese será tu:
```
connector
```
El enunciado especifica que tu observer consume desde observer.X.q, alimentada desde el exchange fulfillment.x.

7. ¿Qué es AMQP?

Para hablar con RabbitMQ existe un protocolo llamado:

AMQP — Advanced Message Queuing Protocol

Puedes verlo como el “idioma” con que tu connector habla con RabbitMQ.
```
connector
    │
    │ AMQP
    ▼
RabbitMQ
```
De manera similar:
```
browser
    │
    │ HTTP
    ▼
FastAPI
```
El enunciado exige explícitamente conectar al broker mediante AMQP.

El broker indicado por la tarea es:
```
broker.iic2173.org:5671
```
y tus credenciales/cola particular te las entregan por Canvas.

8. ¿Qué librería Python usar para RabbitMQ?

Tienes varias opciones.

Yo usaría:
```
aio-pika
```
porque encaja muy bien con Python asíncrono.

Instalación:
```
pip install aio-pika
```
La ventaja especialmente importante para esta tarea es que dispone de:
```
aio_pika.connect_robust(...)
```
para conexiones robustas y reconexión, algo muy pertinente porque el enunciado exige que si RabbitMQ se cae temporalmente, connector no muera permanentemente, sino que vuelva a intentar conectarse.

También existe:

```
pika
```
que es otra biblioteca muy conocida para RabbitMQ.

Para tu proyecto yo elegiría:
```
aio-pika
```
9. Tu connector

Ahora podemos entender el primer programa que vas a escribir.

Será algo como:
```
connector.py
```
Su única misión será:
```
Conectarse a RabbitMQ
        ↓
Esperar mensajes
        ↓
Recibir mensaje
        ↓
Convertir JSON
        ↓
HTTP POST al master
        ↓
Esperar siguiente mensaje
```
Pseudocódigo:
```
while True:

    conectarse_a_rabbitmq()

    for mensaje in cola:

        evento = convertir_json(mensaje)

        enviar_a_master(evento)
```
Pero obviamente tendrás que manejar errores y reconexión.

10. ¿Por qué connector y master deben ser programas separados?

Esta separación es intencional.

Supón que RabbitMQ se cae.

Tu arquitectura debería quedar:
```
RabbitMQ ❌
   │
connector esperando/reconectando
```
pero:
```
Usuario
   │
   ▼
FastAPI
   │
   ▼
Base de datos
```
debe seguir funcionando.

Es decir, todavía puedes consultar todos los mensajes anteriormente almacenados.

El enunciado exige exactamente esta independencia.

Esto es uno de los primeros ejemplos del curso de arquitectura de sistemas:

separar responsabilidades para evitar que la caída de un componente destruya todo el sistema.

11. ¿Qué es HTTP?

HTTP es otro protocolo de comunicación.

Significa:

HyperText Transfer Protocol

Pero no necesitas memorizar el nombre.

Piensa:

HTTP define cómo un programa puede enviarle una solicitud a otro programa a través de una red.

Ejemplo:
```
Cliente                           Servidor
   │
   │ GET /history
   ├──────────────────────────────>
   │
   │      JSON con resultados
   <──────────────────────────────┤
```
12. Métodos HTTP que tienes que entender

Los más conocidos son:
```
GET      obtener información
POST     crear/enviar información
PUT      reemplazar información
PATCH    modificar información
DELETE   eliminar información
```
Para tu tarea principalmente necesitas:
```
GET
POST
```
13. GET

Por ejemplo:

```
GET /history
```
significa:

Dame el historial.

En FastAPI:
```
@app.get("/history")
def obtener_historial():
    return {"mensaje": "historial"}
```
14. POST

POST sirve para enviar información.

Tu connector hará algo conceptualmente como:
```
POST /history
```
con:
```
{
    "idpk": "...",
    "type": "demand-set",
    "packageBody": {...}
}
```
FastAPI lo recibe y lo guarda en la base de datos.

15. ¿Qué significa API?

API significa Application Programming Interface.

En este caso simplemente puedes pensar:

Es un conjunto de rutas que otros programas pueden utilizar para comunicarse con tu aplicación.

Por ejemplo:
```
GET /history
GET /history/15
POST /history
```
Esas rutas se llaman endpoints.

16. ¿Qué significa API REST?

REST es una forma convencional de diseñar APIs HTTP.

Por ejemplo:
```
GET /history
```
trae recursos.
```
GET /history/42
```
trae un recurso específico.

Ese diseño es precisamente el que te pide la tarea.

No necesitas dominar toda la teoría REST para hacer la entrega.

Por ahora entiende:

URL + método HTTP = operación
17. ¿Qué es FastAPI?

FastAPI es un framework web de Python.

Te evita implementar HTTP desde cero.

Por ejemplo:

```
from fastapi import FastAPI

app = FastAPI()

@app.get("/")
def home():
    return {"mensaje": "Hola"}
```
Si ejecutas esto con Uvicorn:
```
uvicorn main:app --reload
```
tienes un servidor HTTP.

Luego:
```
http://localhost:8000/
```
devuelve:
```
{
    "mensaje": "Hola"
}
```
18. FastAPI además te entrega documentación

Una ventaja enorme para esta tarea:
```
http://localhost:8000/docs
```
FastAPI crea automáticamente una interfaz Swagger donde puedes probar:
```
GET /history
GET /history/{id}
POST ...
```
Esto te simplifica muchísimo las pruebas.

19. Path parameters

La tarea exige:
```
/history/{id}
```
Por ejemplo:
```
/history/15
```
En FastAPI:
```
@app.get("/history/{event_id}")
def obtener_evento(event_id: int):
    return {"id": event_id}
```
Aquí:

15

es un path parameter.

20. Query parameters

También te exigen paginación:
```
/history?page=2&limit=25
```
Aquí:
```
page=2
limit=25
```
son query parameters.

FastAPI:
```
@app.get("/history")
def obtener_historial(
    page: int = 1,
    limit: int = 25
):
    ...
```
El enunciado exige que el límite por defecto sea 25.

21. ¿Qué significa paginación?

Imagínate que tienes:

100.000 eventos

No quieres enviar los 100.000 de una vez.

Entonces:
```
page=1 → registros 1-25
page=2 → registros 26-50
page=3 → registros 51-75
```
La propia tarea advierte que recibirán miles de paquetes y que consultar todo de golpe podría tumbar la aplicación.

22. También debes filtrar

Por ejemplo:
```
/history?receivedAt=2025-08-08
```
Debería mostrar únicamente eventos asociados a ese filtro.

La tarea exige poder filtrar por las propiedades de las demandas, con especial atención a los campos temporales.

Por eso tienes que aprender mínimamente:
```
WHERE
LIMIT
OFFSET
```
de SQL.

23. ¿Qué es una base de datos?

Hasta ahora podrías guardar algo en Python:
```
eventos = []
```
pero cuando cierres el programa:

💀 desapareció todo

Necesitas persistencia.

Ahí entra una base de datos.

La tarea permite:
```
PostgreSQL
o
MongoDB
```
24. ¿Cuál te recomiendo?

Yo haría:
```
PostgreSQL
```
por varias razones:

aprenderás SQL;
filtrar fechas es sencillo;
paginar es sencillo;
FastAPI funciona muy bien con PostgreSQL;
es una tecnología extremadamente común.

Para no complicarte demasiado, usaría:
```
FastAPI
+
SQLModel
+
PostgreSQL
```
FastAPI actualmente muestra SQLModel en su documentación oficial; SQLModel se construye sobre SQLAlchemy y Pydantic y soporta PostgreSQL.

25. Modelo básico de la base de datos

Conceptualmente podrías guardar:
```
Event
──────────────────────────────
id
idpk
type
packageBody
validUntil
receivedAt
```

Por ejemplo:
```
id = 1
idpk = "76abe..."
type = "demand-set"
receivedAt = 2026-08-26 20:13
```
Tu:
```
/history/1
```
buscaría ese id.

26. Pydantic

FastAPI utiliza Pydantic para describir y validar datos.

Por ejemplo:
```
from pydantic import BaseModel

class Demand(BaseModel):
    city: str
    demand: float
    unit: str
```
Si llega:
```
{
    "city": "Santiago",
    "demand": 500,
    "unit": "GW"
}
```
funciona.

Si llega:
```
{
    "city": 123,
    "demand": "manzana"
}
```
FastAPI puede detectar que el formato no corresponde.

27. ¿Cómo se comunica connector con master?

Mediante HTTP POST.

Aquí recomiendo:
```
httpx
```
Entonces:
```
import httpx

await client.post(
    "http://master:8000/history",
    json=evento
)
```
Fíjate en algo muy importante:

http://master:8000

y no:

localhost:8000

cuando estén dentro de Docker Compose.

Ya veremos por qué.

28. ¿Qué es Docker?

Supongamos que tu aplicación necesita:
```
Python 3.x
FastAPI
aio-pika
httpx
SQLModel
psycopg
```
En tu computador funciona.

Pero llegas al servidor y:
```
otra versión de Python
otra configuración
faltan paquetes
faltan librerías
```
y deja de funcionar.

Docker intenta solucionar exactamente eso.

La ayudantía lo describe como empaquetar software en un entorno consistente con sus propias dependencias.

29. Imagen Docker vs contenedor

Esta distinción es fundamental.

Imagen

Es una plantilla.
```
Imagen
Python + FastAPI + tu código
Container
```
Es una instancia ejecutándose de esa imagen.
```
Imagen
  │
  ├── Container master
  ├── Container master2
  └── Container master3
```
Analogía:
```
Clase Python → imagen
Objeto Python → container
```
No es exactamente lo mismo, pero sirve para entenderlo.

30. ¿Qué es un Dockerfile?

Es la receta para construir la imagen.

La ayudantía menciona instrucciones como FROM, RUN, WORKDIR, COPY, EXPOSE y CMD.

Algo típico para FastAPI:
```
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```
Luego:
```
docker build -t master .
```
crea la imagen.

Y:
```
docker run master
```
crea un container.

31. Tu tarea tendrá al menos tres containers

Conceptualmente:
```
┌────────────────┐
│ master         │
│ FastAPI        │
└────────────────┘

┌────────────────┐
│ connector      │
│ RabbitMQ       │
│ consumer       │
└────────────────┘

┌────────────────┐
│ db             │
│ PostgreSQL     │
└────────────────┘
```
RabbitMQ no está en tus containers porque el broker lo administra el curso.

32. ¿Qué es Docker Compose?

Sin Compose tendrías que hacer:
```
docker run ...
docker run ...
docker run ...
```
con muchísimas configuraciones.

Docker Compose permite describir todo en:
```
docker-compose.yml
```
Por ejemplo:

services:

  master:
    build: ./master

  connector:
    build: ./connector

  db:
    image: postgres

y levantar todo con:
```
docker compose up
```
La tarea exige que puedas lanzar master, DB y connector desde Docker Compose.

La ayudantía además destaca comandos como:
```
docker compose up
docker compose down
docker compose build
docker compose ps
docker ps
docker logs
```
33. Docker Network

Docker Compose crea una red donde los servicios se reconocen por nombre.

Entonces:
```
connector
   │
   │ http://master:8000
   ▼
master
```
funciona.

Y:
```
master
   │
   │ postgresql://db:5432
   ▼
   db
```
funciona.

Ese detalle es importantísimo.

Dentro de connector:

localhost

significaría:

el propio container connector.

No master.

34. HEALTHCHECK

Tu enunciado exige que todos los containers tengan HEALTHCHECK. Es esencial.

Docker pregunta:

¿Este servicio realmente está funcionando?

Para FastAPI podrías tener:
```
@app.get("/health")
def health():
    return {"status": "ok"}
```
y Docker ejecutar:
```
curl http://localhost:8000/health
```
Para connector, el enunciado permite utilizar una comprobación mediante archivo en vez de crear una API HTTP.

35. ¿Qué significa Cloud?

Ahora salimos de tu PC.

Cuando ejecutas:
```
localhost:8000
```
solo tú puedes acceder cómodamente.

La tarea quiere:
```
Internet
   ↓
tu servidor
   ↓
tu aplicación
```
Cloud computing significa utilizar infraestructura informática remota proporcionada por una empresa.

En tu caso:
```
Amazon Web Services
AWS
```
36. ¿Qué es EC2?

EC2 significa aproximadamente:

computador virtual alquilado dentro de AWS.

Puedes imaginar:
```
AWS
└── EC2
    └── Ubuntu Linux
        ├── Docker
        ├── Docker Compose
        ├── Nginx
        └── tu proyecto
```
Para ti será prácticamente:

un PC Linux remoto que está encendido en Internet.

La tarea exige que tu servidor corra en una instancia EC2 free tier.

Y explícitamente prohíbe alternativas que abstraigan el deployment como Heroku, Elastic Beanstalk, Lightsail, Amplify, etc.

37. ¿Cómo entras a EC2?

Con:
```
SSH
```
La ayudantía lo define como la forma estándar de acceder de forma segura a servidores remotos.

Conceptualmente:
```
Tu computador
     │
     │ SSH
     ▼
    EC2
```
Comando:
```
ssh -i archivo.pem usuario@IP
```
El .pem es tu llave privada.

38. MUY importante con el .pem

Tu enunciado dice literalmente que está absolutamente prohibido subir el .pem a GitHub.

Eso implica nota 1.

Entonces:
```
.pem
```
debe ir en tu .gitignore.

39. ¿Qué es una IP?

Una IP identifica una máquina en una red.

Ejemplo inventado:
```
18.221.15.87
```
Tu EC2 tendrá una.

Entonces inicialmente podrías acceder:
```
http://18.221.15.87
```
40. ¿Qué es una Elastic IP?

Si reinicias ciertos servidores cloud, su dirección pública podría cambiar.

AWS permite asociar una:
```
Elastic IP
```
que mantienes asignada a tu servidor.

Esto es muy importante porque luego:
```
dominio → IP
```
Si la IP cambiara, el dominio dejaría de apuntar correctamente.

La ayudantía propone precisamente:
```
EC2
↓
Elastic IP
↓
Dominio
```
41. ¿Qué son los puertos?

Una misma máquina puede ejecutar muchos servicios.

Entonces existen números de puerto.

Por ejemplo:
```
22     SSH
80     HTTP
443    HTTPS
8000   FastAPI, típicamente
5432   PostgreSQL
```
Puedes imaginar:
```
IP = edificio
puerto = puerta
```
Por ejemplo:
```
18.20.10.4:8000
```
dice:
```
máquina 18.20.10.4, puerta 8000.
```
42. Security Groups de AWS

AWS tiene un firewall.

Tienes que decir qué puertas pueden recibir tráfico.

Para tu aplicación probablemente terminarás necesitando públicamente:
```
22     SSH
80     HTTP
443    HTTPS
```
No deberías exponer PostgreSQL gratuitamente a Internet.

43. ¿Qué es DNS?

Aquí aparece otro problema.

Nadie quiere escribir:

18.221.15.87

Quieres escribir:

energyshark-daniel.me

DNS traduce:

```
energyshark-daniel.me
          ↓
       IP EC2
```

Es básicamente el “directorio telefónico de Internet”.

44. Registro A

La ayudantía explica un registro importante:

A

que asocia:

dominio → IPv4

Ejemplo:

energyshark.me
      ↓
18.221.15.87
45. CNAME

Otro registro es:

CNAME

que asocia un nombre con otro.

Ejemplo:

www.energyshark.me
        ↓
energyshark.me

La ayudantía recomienda justamente configurar un A hacia la Elastic IP y un CNAME para www.

46. ¿Qué es Nginx?

Aquí viene una de las piezas que suele ser confusa al principio.

FastAPI puede correr en:

localhost:8000

pero normalmente no quieres exponer directamente Uvicorn a Internet.

Pones Nginx delante:

```
Internet
   │
   │ puerto 80 / 443
   ▼
 NGINX
   │
   │ localhost:8000
   ▼
FastAPI
```

47. ¿Qué es un reverse proxy?

Nginx recibe la petición:
```
GET https://energyshark.me/history
```
y dice:

esto se lo voy a mandar a FastAPI.

Entonces internamente:
```
Nginx
  ↓
http://localhost:8000/history
```
FastAPI responde y Nginx devuelve la respuesta al usuario.

Eso es un reverse proxy.

El enunciado exige uno y especifica que debe estar configurado directamente en EC2, no dentro de Docker.

48. ¿Por qué Nginx?

Entre otras cosas:

recibe tráfico de Internet;
maneja HTTP;
puede manejar HTTPS;
hace reverse proxy;
puede balancear carga;
evita exponer directamente FastAPI.

La ayudantía también presenta Nginx como web server, load balancer y reverse proxy.

49. ¿HTTP vs HTTPS?

HTTP:
```
Cliente ─── información ─── Servidor
```
La información no tiene la protección criptográfica que entrega TLS.

HTTPS:
```
HTTP + TLS
```
Entonces los datos que viajan entre cliente y servidor van protegidos.

URLs:
```
http://tudominio.com
```
versus:
```
https://tudominio.com
```
Los puertos habituales son:
```
HTTP  → 80
HTTPS → 443
```
50. ¿Qué es SSL/TLS?

Aunque muchas personas dicen “certificado SSL”, actualmente la tecnología relevante es TLS.

El certificado permite que un navegador pueda verificar algo como:

Estoy realmente hablando con energyshark.me.

y establecer comunicación cifrada.

51. Let's Encrypt y Certbot

La tarea permite utilizar:
```
Let's Encrypt
```
para obtener un certificado gratuito.

Y:
```
Certbot
```
para configurarlo/renovarlo.

52. La parte variable de la tarea

Aquí tienes que elegir entre dos opciones.

Opción A — HTTPS

Debes:
```
certificado SSL Let's Encrypt
HTTP → HTTPS
renovación automática 2 veces al día
```

Opción B — Load balancing

Debes ejecutar al menos:
```
master1
master2
```
y:
```
             Nginx
             /   \
            ↓     ↓
        master1  master2
```
¿Cuál haría yo?

Para esta entrega elegiría:
```
HTTPS.
```
El propio enunciado indica que HTTPS será necesario en entregas futuras, por lo que implementarlo ahora te deja avanzado para después.

53. El stack que te recomiendo

Para no mezclar demasiadas tecnologías:
```
Función	Tecnología
Lenguaje	Python
API	FastAPI
Servidor FastAPI	Uvicorn
Validación	Pydantic
RabbitMQ	aio-pika
HTTP connector → master	httpx
Base de datos	PostgreSQL
ORM	SQLModel
Driver PostgreSQL	psycopg
Containers	Docker
Orquestación	Docker Compose
Servidor cloud	AWS EC2 / Ubuntu
Reverse proxy	Nginx
DNS	dominio público
HTTPS	Let's Encrypt + Certbot
```
Tus dependencias Python podrían terminar siendo aproximadamente:
```
fastapi
uvicorn
sqlmodel
psycopg
aio-pika
httpx
```
Y opcionalmente:
```
python-dotenv
pytest
```
54. ¿Qué es un ORM?

Si no usas ORM, escribirías:

```
SELECT *
FROM events
WHERE id = 15;
```

Un ORM permite hacer algo parecido desde Python:

```
evento = session.get(Event, 15)
```

El ORM traduce eso a SQL.

Para una primera entrega, SQLModel te simplifica bastante esta capa. La propia documentación oficial de FastAPI lo presenta como una opción construida sobre SQLAlchemy y Pydantic.

55. ¿Cómo debería quedar organizado tu proyecto?

Yo apuntaría aproximadamente a:

```
energyshark/
│
├── master/
│   ├── main.py
│   ├── models.py
│   ├── database.py
│   ├── schemas.py
│   ├── Dockerfile
│   └── requirements.txt
│
├── connector/
│   ├── main.py
│   ├── Dockerfile
│   └── requirements.txt
│
├── docker-compose.yml
│
├── nginx/
│   └── energyshark.conf
│
├── ai-docs/
│   └── prompts/
│
├── .env.example
├── .gitignore
└── README.md
```

No necesitas comenzar creando todo esto. Esa sería más bien la forma final.

56. El flujo exacto de un evento

Quiero que esta parte te quede muy clara.

Supongamos que la central genera:
```
{
    "idpk": "abc123",
    "type": "demand-set",
    ...
}
```
Paso A

RabbitMQ recibe el evento.
```
Central
   ↓
RabbitMQ
```
Paso B

RabbitMQ lo deja en:
```
observer.TU_NUMERO.q
```
Paso C

connector consume el mensaje.
```
RabbitMQ
   ↓ AMQP
connector
Paso D
```
Python convierte el JSON.
```
evento = json.loads(message.body)
```
Paso E

Connector llama:
```
POST http://master:8000/history
```
Paso F

FastAPI valida el evento.
```
Pydantic
```
Paso G

Agrega:
```
receivedAt
```
Paso H

Lo guarda:
```
PostgreSQL
```
Paso I

Posteriormente alguien solicita:
```
GET /history
```
Paso J

FastAPI consulta PostgreSQL.

Paso K

Devuelve JSON.

Ese es prácticamente el corazón completo de tu E0.

57. Lo que la API debe ofrecer

Según tu enunciado, como mínimo necesitas:
```
GET /history
```
lista de eventos.
```
GET /history/{id}
```
evento particular.
```
GET /history?page=2&limit=25
```
paginación.

Y filtros como:
```
GET /history?receivedAt=2025-08-08
```
además de filtrado por las demás propiedades.

También necesitarás internamente algún:
```
POST ...
```
para que connector entregue los mensajes a master; la ruta concreta de ese POST la puedes diseñar tú.

58. Qué pasa si RabbitMQ se cae

Esto es MUY importante en la corrección.

Mal:
```
connection = rabbitmq.connect()

# falla
```
programa termina 💀

Bien:
```
Intento conectar
     ↓
¿funciona?
 ├─ sí → consumir
 └─ no
      ↓
    esperar
      ↓
    reintentar
```
El enunciado exige que connector intente reconectarse sin intervención manual.

Aquí aio-pika.connect_robust() resulta particularmente útil porque está diseñado para mantener/reestablecer conexiones robustas.

59. ¿Y si RabbitMQ está caído pero un usuario consulta?

Esto:

RabbitMQ ❌

no debería provocar:

GET /history ❌

Porque master tiene:

PostgreSQL

y puede seguir devolviendo eventos anteriores.

Esta es una demostración muy concreta de por qué la arquitectura tiene componentes separados.

60. ¿En qué orden deberías aprender y hacer la tarea?

Aquí no seguiría ciegamente el orden de archivos. Haría un desarrollo incremental parecido al roadmap del enunciado, pero un poco más detallado para alguien que está aprendiendo desde cero. El roadmap oficial propone: probar RabbitMQ, levantar servidor HTTP, containerizar, crear EC2, instalar Docker, desplegar, configurar DNS y terminar la configuración.

Etapa 1 — HTTP y FastAPI local

Antes de tocar AWS, RabbitMQ o Docker:
```
@app.get("/")
```
Haz funcionar:
```
localhost:8000
```
Luego aprende:
```
GET
POST
path parameters
query parameters
JSON
status codes
```
Etapa 2 — Construye /history sin base de datos

Temporalmente:
```
eventos = []
```
Implementa:
```
POST /history
GET /history
GET /history/{id}
```
Así entiendes primero la API.

Etapa 3 — Aprende PostgreSQL

Reemplaza:
```
eventos = []
```
por:
```
PostgreSQL
```
Aprende solamente:
```
INSERT
SELECT
WHERE
LIMIT
OFFSET
```
No necesitas convertirte en experto SQL.

Etapa 4 — Implementa paginación

Haz funcionar:
```
/history?page=1&limit=25
```
y luego:
```
/history?page=2&limit=25
```
Etapa 5 — Implementa filtros

Primero:
```
receivedAt
```
después:
```
idpk
type
validUntil
...
```
Etapa 6 — Haz una prueba sencilla con RabbitMQ

Crea connector.py.

Primero solo:
```
RabbitMQ
   ↓
print(mensaje)
```
Si ves en consola los eventos, acabas de solucionar una parte enorme.

El propio roadmap oficial comienza justamente con una prueba de concepto del cliente RabbitMQ.

Etapa 7 — Haz RabbitMQ → HTTP

Después:
```
RabbitMQ
   ↓
connector
   ↓ POST
FastAPI
```
Todavía todo local.

Etapa 8 — Implementa reconexión

Apaga o simula una caída de RabbitMQ.

Tu connector debe:
```
fallar
esperar
reintentar
recuperarse
```
sin intervención manual.

Etapa 9 — Dockeriza master

Crea:
```
master/Dockerfile
```
y comprueba:
```
docker build
docker run
```
Etapa 10 — Dockeriza connector

Haz exactamente lo mismo.

Etapa 11 — Docker Compose

Ahora unes:
```
master
connector
postgres
```
con:
```
docker compose up
```
Y comprueba:
```
connector → master
master → db
```
Etapa 12 — HEALTHCHECK

Añade healthcheck a cada container.

No lo dejes para el final porque es requisito esencial.

Etapa 13 — AWS EC2

Crea EC2.

Aprende:
```
instance
IP
Elastic IP
Security Groups
SSH
.pem
```
Etapa 14 — Instala Docker en EC2

La ayudantía entrega incluso el procedimiento para Ubuntu: actualizar paquetes, instalar dependencias, agregar el repositorio de Docker e instalar Docker Engine + Compose.

Etapa 15 — Despliega temprano

El enunciado insiste en esto y tiene razón:

NO esperes a terminar todo localmente.

En cuanto puedas, lleva una versión mínima a EC2.

La pauta advierte explícitamente que no se corregirán entregas locales y que hay que avanzar incrementalmente en cloud.

Etapa 16 — Nginx

Instálalo directamente en EC2:
```
Internet
   ↓
Nginx :80
   ↓
master :8000
```
La ayudantía resume el deployment como: servidor levantado, acceso al puerto 80, SSH, clonar repositorio, instalar Docker, configurar Nginx y ejecutar la aplicación.

Etapa 17 — Elastic IP + dominio

Configura:
```
Dominio
   ↓ registro A
Elastic IP
   ↓
EC2
```
y:
```
www
 ↓ CNAME
dominio principal
```
Etapa 18 — HTTPS

Una vez que:
```
http://tudominio.com
```
funcione, recién entonces configura:
```
Certbot
Let's Encrypt
HTTPS
```
No intentaría HTTPS antes de tener HTTP funcionando.

Etapa 19 — Prueba todo como si fueras el ayudante

Debes poder apagar/reiniciar cosas y comprobar:
```
docker compose ps
docker logs ...
curl ...
```
y verificar:

dominio → Nginx → master → PostgreSQL
RabbitMQ → connector → master → PostgreSQL
61. Qué NO deberías intentar aprender todavía

Para esta entrega yo no me distraería con:
```
Kubernetes
AWS ECS
microservices avanzados
Kafka
Redis
CI/CD complejo
Terraform
serverless
GraphQL
OAuth
frontend
React
```
La ayudantía menciona algunas tecnologías más avanzadas, pero tu tarea puede resolverse perfectamente sin ellas. Docker Compose es suficiente para la composición requerida.

62. Hay cuatro “mundos” que debes separar mentalmente

Esta forma de verlo suele ayudar mucho:
```
MUNDO 1 — SOFTWARE
Python
FastAPI
Pydantic
SQLModel
aio-pika
httpx

MUNDO 2 — DATOS
JSON
RabbitMQ
PostgreSQL

MUNDO 3 — DEPLOYMENT
Docker
Docker Compose
EC2
SSH

MUNDO 4 — INTERNET
IP
puertos
DNS
Nginx
HTTP
HTTPS
TLS
```
Al principio parecen 20 cosas diferentes.

En realidad son solo cuatro capas que se van conectando.

63. La arquitectura que yo usaría

Para tu caso concreto:

                        INTERNET
                            │
                       HTTPS :443
                            │
                            ▼
                    ┌──────────────┐
                    │    NGINX     │
                    │  EC2 host    │
                    └──────┬───────┘
                           │
                           │ HTTP
                           ▼

           DOCKER NETWORK EN AWS EC2
```
┌────────────────────────────────────────────┐
│                                            │
│          ┌─────────────────────┐           │
│          │       master        │           │
│          │ FastAPI + SQLModel  │           │
│          │      :8000          │           │
│          └─────────┬───────────┘           │
│                    │                       │
│                    ▼                       │
│          ┌─────────────────────┐           │
│          │     PostgreSQL      │           │
│          │       :5432         │           │
│          └─────────────────────┘           │
│                                            │
│          ┌─────────────────────┐           │
│          │      connector      │           │
│          │ aio-pika + httpx    │           │
│          └─────────┬───────────┘           │
│                    │                       │
└────────────────────┼───────────────────────┘
                     │
                     │ AMQP
                     ▼
              RabbitMQ del curso
              observer.X.q
```

Si logras entender este dibujo, ya entiendes aproximadamente el 80 % conceptual de la tarea.

64. Qué es obligatorio y qué no

Hay varios requisitos Esenciales que debes tratar como prioridad absoluta:
```
✓ API /history
✓ paginación
✓ filtros
✓ connector independiente
✓ RabbitMQ mediante AMQP
✓ reconexión automática
✓ master containerizado
✓ connector containerizado
✓ misma Docker network
✓ EC2
✓ HEALTHCHECK en containers
```
Y además se evalúan:
```
proxy inverso
dominio
PostgreSQL/Mongo
Docker Compose
```
No es una buena estrategia empezar por puntos cosméticos. Aquí hay muchos más puntos en infraestructura que en “hacer endpoints”.

65. Dos advertencias del enunciado que no debes olvidar
.pem

Nunca:
```
git add servidor.pem
```
Está expresamente prohibido y causa rechazo de la entrega.

Uso de ChatGPT

Esto también es particularmente relevante ahora mismo: el enunciado permite AI, pero exige documentarla.

Si haces consultas a ChatGPT/Claude, debes guardar los prompts y respuestas en:
```
ai-docs/prompts
```
y si usas un IDE asistido por IA debes conservar las interacciones relacionadas con la entrega.

Por lo tanto, esta conversación que estamos teniendo deberías conservarla dentro de esa documentación.

66. Tu mapa de estudio

Para que no te abrumes, yo estudiaría en este orden:
```
Python básico
      ↓
JSON
      ↓
HTTP
      ↓
REST
      ↓
FastAPI
      ↓
PostgreSQL + SQLModel
      ↓
RabbitMQ + colas + AMQP
      ↓
aio-pika
      ↓
connector → master
      ↓
Docker
      ↓
Docker Compose
      ↓
Linux / SSH
      ↓
AWS EC2
      ↓
IP + puertos
      ↓
DNS
      ↓
Nginx
      ↓
HTTPS / TLS / Certbot
```
No intentes aprender Docker, AWS, Nginx, RabbitMQ y FastAPI simultáneamente. Ahí es donde todo empieza a parecer incomprensible.

Y cómo te propongo seguir

En nuestro siguiente paso, yo no empezaría todavía con AWS ni RabbitMQ.

Empezaría contigo desde:

PARTE 1
```
HTTP + API REST + FastAPI
```
y construiríamos localmente algo extremadamente sencillo:
```
POST /history
GET /history
GET /history/{id}
```
primero sin PostgreSQL, sin Docker y sin RabbitMQ.

Cuando entiendas exactamente qué es un request, un response, GET, POST, endpoint, path parameter, query parameter y JSON, agregamos PostgreSQL. Después RabbitMQ. Después Docker. Y solo entonces AWS/Nginx/DNS.

Ese enfoque además sigue bastante bien el espíritu del roadmap que entrega el propio curso: construir piezas pequeñas, probarlas y avanzar incrementalmente hacia producción.