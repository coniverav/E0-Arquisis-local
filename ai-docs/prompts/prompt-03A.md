## Prompt

Perfecto. Ahora te daré la información de mis credenciales para resolver la tarea, necesito que, considerando todo lo que me explicaste de cómo hacerlo, lo apliques y me lo des en un archivo .zip que pueda usar localmente (hasta la etapa 12). Quiero que seas meticuloso en cumplir todo lo del enunciado y que hagas AMBAS partes del ítem variable. Se conciso con las soluciones de código, pero agrega todos los comentarios que creas necesarios para poder entender los códigos y su funcionamiento. Asimismo, dame las instrucciones de cómo debo ejecutarlo en local para probarlo y verificar que funciona bien hasta esa etapa (luego te pediré ayuda con la subida a AWS y los pasos siguientes para terminar la tarea).

Mis credenciales (para el .env) son: xxx

## Respuesta

Perfecto. Preparé el proyecto completo en .zip para trabajar localmente hasta la etapa 12, usando Python + FastAPI, PostgreSQL, aio-pika, httpx, Docker y Docker Compose.

Descargar EnergyShark E0 — etapa local 1–12

Qué dejé implementado

La solución incluye master, master2, connector y db como containers separados, todos con HEALTHCHECK. connector consume tu cola mediante AMQPS con verificación TLS activa, no intenta declarar/modificar la cola ni el exchange, reintenta la conexión si RabbitMQ falla y solo hace ACK después de que master haya persistido el evento. Esto apunta directamente a los RNF esenciales del enunciado.

La API incluye:
```
GET /history
GET /history/{id}
POST /internal/events para connector
paginación page + limit, con 25 por defecto
filtros por id, idpk, type, receivedAt, validUntil, metaContent, constraints, city, demand y unit
receivedAt generado al recibir el evento
protección contra duplicados mediante idpk
Swagger en /docs
```
Eso cubre los requisitos funcionales de historial, detalle, paginación y filtrado.

También preparé AMBAS partes variables

Para balanceo, docker-compose.yml levanta dos instancias reales:
```
master  → localhost:8001
master2 → localhost:8002
```
ambas comparten PostgreSQL. Además agregué un header:
```
X-EnergyShark-Instance
```
para que después puedas demostrar que Nginx distribuye peticiones entre ambas. La configuración Nginx incluida ya tiene los dos upstreams, que es exactamente lo que pide esa parte variable.

Para HTTPS, incluí en deploy/ la configuración y script para:
```
Let's Encrypt
Certbot
HTTP → HTTPS
TLS 1.2 / 1.3
renovación automática a las 00:00 y 12:00
```
Esto deja implementada la configuración, pero no puede quedar operacional todavía en localhost: Let's Encrypt necesita un dominio público apuntando a tu EC2, y el enunciado exige que Nginx esté directamente instalado en EC2, no dentro de Docker. Lo terminaremos cuando hagamos AWS.

Cómo probarlo ahora

Descomprime el ZIP, abre PowerShell/terminal dentro de energyshark_e0_local y ejecuta:
```
docker compose up -d --build
```
Luego:
```
docker compose ps
```
Después de unos segundos deberías tener:
```
db          healthy
master      healthy
master2     healthy
connector   healthy
```
Ejecuta la prueba automática:
```
python scripts/smoke_test.py
```
Debería terminar con:

Smoke test completado correctamente.

Luego puedes entrar a:
```
http://localhost:8001/docs
http://localhost:8002/docs
```
y probar manualmente FastAPI.

Para verificar RabbitMQ:
```
docker compose logs -f connector
```
Deberías ver mensajes indicando que se está conectando y esperando eventos en tu cola. En el código usé get_queue(..., ensure=False), que obtiene la cola sin realizar queue.declare, importante porque tus credenciales no tienen permiso para modificar la topología. La documentación actual de aio-pika confirma precisamente ese comportamiento de ensure=False.

Dos advertencias importantes

El .env del ZIP sí contiene las credenciales privadas que me entregaste, tal como pediste. Ya está incluido en .gitignore; no subas .env ni este ZIP completo a un repositorio público. Para GitHub está también .env.example, que no contiene la contraseña.

Además, cuando ejecutes connector contra RabbitMQ real, cada evento correctamente procesado recibe ACK y sale de tu cola. Es decir:
```
RabbitMQ → connector → master → PostgreSQL local
                     ✓
                    ACK
```
Así que esos mensajes quedarán en tu PostgreSQL local, no esperando posteriormente en RabbitMQ. No ejecutes dos connectors simultáneamente con la misma cola.

También dejé ai-docs/prompts/, porque el enunciado exige registrar el uso de IA.

No pude ejecutar los containers en mi entorno porque aquí no está disponible Docker Engine, pero sí hice validaciones estáticas del proyecto: todos los Python parsean correctamente, docker-compose.yml es YAML válido, el script Bash es sintácticamente válido y el ZIP pasó verificación de integridad.

Cuando lo pruebes, si algo falla, mándame primero la salida de:
```
docker compose ps
```
y luego:
```
docker compose logs connector
docker compose logs master
```
y lo corregimos antes de pasar a EC2 → Elastic IP → DNS → Nginx → HTTPS → balanceo real.