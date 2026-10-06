# EnergyShark - Entrega 1 ⚡🦈

## Arquitectura y despliegue

La Entrega 1 se ejecuta completamente en cloud y separa el frontend del backend.

El flujo principal es:

```text
Frontend SPA
   ↓
Amazon S3
   ↓
CloudFront
   ↓
Auth0
   ↓
AWS API Gateway
   ↓
https://api.energyshark-g10.tech
   ↓
Nginx
   ↓
EC2
   ↓
Docker Compose
   ├── master
   ├── master2
   ├── cycle-scheduler
   ├── connector
   ├── migrate
   └── PostgreSQL
```

Las imágenes propias utilizadas en producción son construidas fuera de EC2, publicadas en Amazon ECR y posteriormente descargadas por la instancia mediante `docker-compose.prod.yml`.

La observabilidad se realiza con New Relic:

```text
master / master2
   ↓
New Relic APM

EC2 + Docker
   ↓
New Relic Infrastructure
```

---

## Servicios principales

| Servicio | Responsabilidad |
|---|---|
| `db` | PostgreSQL compartido por las instancias del backend. |
| `migrate` | Ejecuta `alembic upgrade head` antes de iniciar la API. |
| `master` | Primera instancia de FastAPI. |
| `master2` | Segunda instancia de FastAPI. |
| `cycle-scheduler` | Worker encargado del procesamiento temporal de ciclos. |
| `connector` | Consume RabbitMQ y comunica los mensajes con el backend. |
| `newrelic-infra` | Monitorea la infraestructura EC2 y los contenedores Docker. |

---

## Variables de entorno

Entre las variables productivas se encuentran:

```text
POSTGRES_DB
POSTGRES_USER
POSTGRES_PASSWORD
DATABASE_URL

RABBITMQ_URL
RABBITMQ_QUEUE
RABBITMQ_OUTBOUND_EXCHANGE
RABBITMQ_CENTRAL_ROUTING_KEY

CITY_ID

AUTH_JWKS_URL
AUTH_AUDIENCE
AUTH_ISSUER

CORS_ALLOWED_ORIGINS

ECR_REPOSITORY
IMAGE_TAG

NEW_RELIC_LICENSE_KEY
NEW_RELIC_APP_NAME
NEW_RELIC_MONITOR_MODE
NEW_RELIC_LOG
NEW_RELIC_INFRA_DISPLAY_NAME
```

## Ejecución local

Desde la raíz del repositorio:

```bash
cd ~/E0-Arquisis-local

docker compose down --remove-orphans
docker compose up -d --build

docker compose ps -a
```

El stack local utiliza `docker-compose.yml`, el cual construye las imágenes directamente desde el código fuente.

Para comprobar el backend:

```bash
curl http://localhost:8001/health
```

La respuesta esperada es similar a:

```json
{
  "status": "ok",
  "instance": "master"
}
```

---

## Pruebas

Para ejecutar la suite completa:

```bash
cd ~/E0-Arquisis-local

docker compose down --remove-orphans
docker compose up -d --build

docker compose ps -a

docker compose exec master python -m pytest -v
```

También se puede ejecutar la suite utilizando un contenedor efímero:

```bash
docker compose build master
docker compose run --rm master pytest -q
```

Para ejecutar un archivo de pruebas específico:

```bash
docker compose exec master \
python -m pytest tests/test_xxxxxxxxxxxxxxx.py -v
```

---

## Docker Compose de producción

El repositorio mantiene dos configuraciones separadas:

```text
docker-compose.yml
→ desarrollo/local
→ utiliza build:

docker-compose.prod.yml
→ producción
→ utiliza imágenes publicadas en ECR
```

El compose productivo no construye el backend dentro de EC2.

`master`, `master2`, `migrate` y `cycle-scheduler` utilizan:

```text
${ECR_REPOSITORY}:api-${IMAGE_TAG}
```

Mientras que `connector` utiliza:

```text
${ECR_REPOSITORY}:connector-${IMAGE_TAG}
```

PostgreSQL utiliza directamente:

```text
postgres:16-alpine
```

Para validar la configuración sin levantar servicios:

```bash
docker compose -f docker-compose.prod.yml config
```

Para listar las imágenes resueltas:

```bash
docker compose -f docker-compose.prod.yml config --images
```

---

## Amazon ECR

Región utilizada:

```text
us-east-2
```

Repositorio ECR:

```text
521294961974.dkr.ecr.us-east-2.amazonaws.com/energyshark-backend
```

Se utilizan tags asociados al commit desplegado:

```text
api-<sha>
connector-<sha>
```

Ejemplo:

```text
api-d903317
connector-d903317
```

### Autenticación en ECR

```bash
aws ecr get-login-password --region us-east-2 \
  | docker login \
    --username AWS \
    --password-stdin \
    521294961974.dkr.ecr.us-east-2.amazonaws.com
```

### Build de las imágenes

```bash
TAG=$(git rev-parse --short HEAD)

docker build \
  --platform linux/amd64 \
  -t 521294961974.dkr.ecr.us-east-2.amazonaws.com/energyshark-backend:api-$TAG \
  ./master
```

```bash
docker build \
  --platform linux/amd64 \
  -t 521294961974.dkr.ecr.us-east-2.amazonaws.com/energyshark-backend:connector-$TAG \
  ./connector
```

### Push a ECR

```bash
docker push \
  521294961974.dkr.ecr.us-east-2.amazonaws.com/energyshark-backend:api-$TAG
```

```bash
docker push \
  521294961974.dkr.ecr.us-east-2.amazonaws.com/energyshark-backend:connector-$TAG
```

Para comprobar las imágenes:

```bash
aws ecr describe-images \
  --repository-name energyshark-backend \
  --region us-east-2 \
  --output json
```

---

## Despliegue en EC2

La aplicación productiva se ejecuta en una instancia EC2 en `us-east-2`.

IP pública utilizada actualmente:

```text
3.142.27.156
```

El repositorio se encuentra en:

```text
/opt/energyshark
```

El `.env` productivo se encuentra únicamente en la instancia y no está versionado.

### Actualizar el código

```bash
cd /opt/energyshark

git checkout develop
git pull --ff-only origin develop
```

### Seleccionar versión

En `.env`:

```text
ECR_REPOSITORY=521294961974.dkr.ecr.us-east-2.amazonaws.com/energyshark-backend
IMAGE_TAG=<sha>
```

### Descargar imágenes

```bash
docker compose -f docker-compose.prod.yml pull
```

### Levantar el stack

```bash
docker compose -f docker-compose.prod.yml up -d
```

### Revisar estado

```bash
docker compose -f docker-compose.prod.yml ps -a
```

Para verificar qué imagen está utilizando una instancia:

```bash
docker inspect master --format '{{.Config.Image}}'
```

Debe mostrar una imagen proveniente de ECR, por ejemplo:

```text
521294961974.dkr.ecr.us-east-2.amazonaws.com/energyshark-backend:api-<sha>
```

---

## API pública

La API productiva está disponible mediante:

```text
https://api.energyshark-g10.tech
```

La API se encuentra detrás de AWS API Gateway.

API Gateway:

```text
API ID: ayv7o6rl9l
Región: us-east-2
Stage: $default
```

Las rutas públicas expuestas son:

```text
GET /health
GET /cycles
GET /cycles/{cycle_id}
GET /connectivity
GET /audit/anomalies
```

Las rutas de negociaciones requieren autenticación:

```text
GET  /negotiations
GET  /negotiations/{negotiation_id}
POST /negotiations
```

No se exponen mediante API Gateway las rutas internas del backend.

Para comprobar la API:

```bash
curl -i https://api.energyshark-g10.tech/health
```

```bash
curl -i https://api.energyshark-g10.tech/cycles
```

---

## Auth0

La autenticación utiliza Auth0 con JWT y JWK estándar.

Configuración principal:

```text
Issuer:
https://dev-rz37e3yw6i3fn2bn.us.auth0.com/

Audience:
https://arquisis-e1-api/

JWKS:
https://dev-rz37e3yw6i3fn2bn.us.auth0.com/.well-known/jwks.json
```

API Gateway utiliza un JWT Authorizer para proteger las operaciones de negociaciones.

El header utilizado es:

```http
Authorization: Bearer <access_token>
```

Durante las pruebas end-to-end se verificó que una solicitud autenticada a:

```text
POST /negotiations
```

alcanzara correctamente el backend. Una respuesta `422` por ciclo expirado constituye una respuesta de negocio posterior a la autenticación, no un fallo del authorizer.

---

## CORS

API Gateway permite requests desde el frontend desplegado.

Entre los orígenes configurados se encuentra la distribución CloudFront del frontend.

Se permiten:

```text
GET
POST
OPTIONS
```

Headers utilizados:

```text
Authorization
Content-Type
```

El preflight de una negociación debe responder:

```text
OPTIONS /negotiations → 204
```

---

## Frontend desplegado

El frontend se mantiene en un repositorio separado.

El build productivo se distribuye mediante:

```text
Amazon S3
→ CloudFront
→ HTTPS
```

Bucket:

```text
energyshark-g10-frontend
```

Distribución CloudFront:

```text
ID:
EFUBNHG9ZB6SV

Dominio:
https://d9yjiq237jfab.cloudfront.net
```

La SPA consume la API mediante:

```text
https://api.energyshark-g10.tech
```

El bucket S3 no requiere Static Website Hosting público; CloudFront accede al bucket mediante el origen S3 correspondiente.

---

## New Relic APM

Las instancias HTTP `master` y `master2` están instrumentadas mediante el agente Python de New Relic.

En producción Uvicorn se inicia mediante:

```text
newrelic-admin run-program uvicorn
```

La configuración se entrega mediante variables de entorno:

```text
NEW_RELIC_LICENSE_KEY
NEW_RELIC_APP_NAME
NEW_RELIC_MONITOR_MODE
NEW_RELIC_LOG
```

La license key real no se versiona.

La aplicación aparece en New Relic como:

```text
EnergyShark Backend
```

En New Relic APM se pueden comprobar transacciones como:

```text
app.main:health
app.routers.cycles:list_cycles
app.routers.connectivity:get_connectivity
```

Esto permite verificar que requests reales están siendo recibidas y monitoreadas por APM.

---

## New Relic Infrastructure

El monitoreo de infraestructura se ejecuta como un servicio adicional de Docker Compose:

```text
newrelic-infra
```

Imagen:

```text
newrelic/infrastructure:latest
```

El agente observa el host EC2 y los contenedores Docker.

Para comprobar que está ejecutándose:

```bash
docker compose -f docker-compose.prod.yml ps newrelic-infra
```

Para revisar sus logs:

```bash
docker logs newrelic-infra --tail 100
```

Una inicialización correcta incluye mensajes similares a:

```text
New Relic infrastructure agent is running.
docker sampler enabled
connect got id
```

En New Relic Infrastructure el host aparece como:

```text
EnergyShark EC2
```

y reporta, entre otras métricas:

```text
CPU
memoria
almacenamiento
utilización de disco
contenedores Docker
```