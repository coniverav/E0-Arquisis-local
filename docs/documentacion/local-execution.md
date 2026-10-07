# Ejecución local de EnergyShark

## Objetivo

Este documento describe cómo levantar EnergyShark desde cero en ambiente local.

La aplicación está separada en dos repositorios independientes:

- Backend: `E0-Arquisis-local`
- Frontend: `Arquisis-G10-Frontend`

El backend expone una API JSON mediante FastAPI y el frontend corresponde a una SPA desarrollada con React y Vite.

> Los archivos `.env`, certificados `.pem`, llaves `.key` y otras credenciales reales no se mostrarán. Cada repositorio incluye un `.env.example` con la configuración de referencia.

---

## 1. Requisitos previos

Se requiere contar con:

- Git
- Docker
- Docker Compose
- Node.js
- npm
- Credenciales de RabbitMQ asignadas al equipo
- Configuración de Auth0 utilizada por EnergyShark

Verificar las herramientas instaladas:

```bash
git --version
docker --version
docker compose version
node --version
npm --version
```

---

## 2. Backend

### 2.1 Clonar el repositorio

```bash
git clone git@github.com:coniverav/E0-Arquisis-local.git
cd E0-Arquisis-local
```

### 2.2 Crear el archivo de entorno

Copiar el archivo de ejemplo:

```bash
cp .env.example .env
```

Completar en `.env` los valores privados requeridos. Las variables definidas actualmente por el proyecto son:

```text
POSTGRES_DB
POSTGRES_USER
POSTGRES_PASSWORD
DATABASE_URL

RABBITMQ_URL
RABBITMQ_QUEUE
CITY_ID
RABBITMQ_OUTBOUND_EXCHANGE
RABBITMQ_CENTRAL_ROUTING_KEY

MASTER_URL
MASTER_ERROR_URL
MASTER_AUDIT_URL
MASTER_OUTBOUND_AUDIT_URL
MASTER_OUTBOUND_DISPATCH_URL

CYCLE_REPORT_WINDOW_SECONDS
CYCLE_SCHEDULER_POLL_SECONDS
OUTBOUND_POLL_SECONDS

CONNECTOR_RETRY_SECONDS
HTTP_RETRY_SECONDS
HTTP_TIMEOUT_SECONDS

DOMAIN
LETSENCRYPT_EMAIL
CORS_ALLOWED_ORIGINS

AUTH_JWKS_URL
AUTH_AUDIENCE
AUTH_ISSUER

NEW_RELIC_LICENSE_KEY
NEW_RELIC_APP_NAME
NEW_RELIC_MONITOR_MODE
NEW_RELIC_LOG
NEW_RELIC_INFRA_DISPLAY_NAME
```

Para desarrollo local, la configuración de referencia utiliza:

```env
CORS_ALLOWED_ORIGINS=http://localhost:5173
CYCLE_REPORT_WINDOW_SECONDS=300
CYCLE_SCHEDULER_POLL_SECONDS=5
OUTBOUND_POLL_SECONDS=2
```

Las credenciales reales del broker y de los servicios externos deben completarse únicamente en `.env`.

### 2.3 Levantar el backend

Desde la raíz del repositorio:

```bash
docker compose down --remove-orphans
docker compose up -d --build
docker compose ps -a
```

El ambiente local utiliza `docker-compose.yml` y levanta los siguientes servicios principales:

| Servicio | Responsabilidad |
|---|---|
| `db` | Base de datos PostgreSQL. |
| `migrate` | Ejecuta las migraciones Alembic antes de iniciar la API. |
| `master` | Primera instancia de FastAPI. |
| `master2` | Segunda instancia de FastAPI. |
| `cycle-scheduler` | Worker encargado del procesamiento temporal de los ciclos. |
| `connector` | Consume RabbitMQ y comunica los mensajes con el backend. |

### 2.4 Verificar el backend

La primera instancia del backend queda disponible localmente en:

```text
http://localhost:8001
```

Health check:

```bash
curl http://localhost:8001/health
```

Respuesta esperada:

```json
{
  "status": "ok",
  "instance": "master"
}
```

También se puede comprobar el historial de ciclos:

```bash
curl http://localhost:8001/cycles
```

### 2.5 Revisar logs

API:

```bash
docker compose logs -f master
```

Connector:

```bash
docker compose logs -f connector
```

Scheduler:

```bash
docker compose logs -f cycle-scheduler
```

Para salir de los logs utilizar `Ctrl+C`.

### 2.6 Ejecutar pruebas del backend

Suite completa:

```bash
docker compose exec master python -m pytest -v
```

Alternativamente:

```bash
docker compose build master
docker compose run --rm master pytest -q
```

Para ejecutar un archivo específico:

```bash
docker compose exec master \
  python -m pytest tests/<archivo_test>.py -v
```

---

## 3. Frontend

### 3.1 Clonar el repositorio

En otra terminal:

```bash
git clone git@github.com:VicenteIgnacioSotoGonzalez/Arquisis-G10-Frontend.git
cd Arquisis-G10-Frontend
```

### 3.2 Crear el archivo de entorno

```bash
cp .env.example .env
```

El `.env.example` del frontend contiene las variables públicas requeridas por Vite y Auth0:

```env
VITE_API_URL=http://localhost:8001
VITE_AUTH0_DOMAIN=dev-rz37e3yw6i3fn2bn.us.auth0.com
VITE_AUTH0_CLIENT_ID=pepHXeaHaTA575ELoPdELGOJ20ZIve3M
VITE_AUTH0_AUDIENCE=https://arquisis-e1-api/
```

### 3.3 Instalar dependencias

```bash
npm ci
```

### 3.4 Levantar el frontend

```bash
npm run dev -- --port 5173 --strictPort
```

Abrir:

```text
http://localhost:5173
```

El origen `http://localhost:5173` debe estar permitido en la configuración CORS del backend y en la configuración de Auth0 utilizada para desarrollo.

### 3.5 Validaciones del frontend

```bash
npm run lint
npm run test
npm run build
```

---

## 4. Verificación end-to-end local

Con backend y frontend activos:

1. Abrir `http://localhost:5173`.
2. Iniciar sesión mediante Auth0 cuando corresponda.
3. Verificar que el historial de ciclos cargue información desde la API real.
4. Abrir el detalle de un ciclo y revisar su ledger.
5. Revisar la vista de conectividad.
6. Revisar la creación y seguimiento de negociaciones voluntarias.
7. Revisar la vista de anomalías y auditoría de mensajes.

La pestaña **Network** de las herramientas de desarrollo del navegador permite comprobar las llamadas HTTP realizadas por la SPA.

---

## 5. Ambiente productivo

Frontend:

```text
https://app.energyshark-g10.tech
```

API:

```text
https://api.energyshark-g10.tech
```

Verificación de la API:

```bash
curl -i https://api.energyshark-g10.tech/health
```

El backend productivo utiliza `docker-compose.prod.yml`, mientras que el ambiente local utiliza `docker-compose.yml`.

En producción, los servicios propios del backend se despliegan mediante imágenes almacenadas en Amazon ECR. El frontend se distribuye mediante Amazon S3 y CloudFront.

---

## 6. Detener el ambiente local

Frontend:

```text
Ctrl+C
```

Backend:

```bash
docker compose down
```

---

## 7. Archivos sensibles

No deben versionarse:
```text
.env
```

El archivo permitido para documentar la configuración es:

```text
.env.example
```

Antes de realizar un commit o push se recomienda revisar:

```bash
git status
git diff --cached --name-only
```

Ningún archivo con credenciales reales debe aparecer como archivo versionado o staged.
