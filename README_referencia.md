# EnergyShark — Entrega 0 (Python + FastAPI)

Implementación local hasta Docker/Compose/HEALTHCHECK, con la infraestructura de **ambas partes variables** preparada para el posterior deployment en EC2.

## Arquitectura local

```text
RabbitMQ del curso (AMQPS)
          |
          v
     connector
          | HTTP POST
          v
        master  <----> PostgreSQL <----> master2
      :8001 host                    :8002 host
```

`connector`, `master`, `master2` y `db` comparten `energyshark_net`. Nginx **no** va en Docker: los templates están en `deploy/` para instalarlo directamente en EC2, como exige el enunciado.

## 1. Antes de ejecutar

Necesitas:
- Docker Desktop en Windows (o Docker Engine + Compose en Linux).
- Python 3 únicamente para ejecutar el smoke test; no necesitas instalar FastAPI en tu PC.

**IMPORTANTE:** este ZIP incluye un `.env` privado listo para uso local. Está ignorado por Git. No lo subas ni lo compartas.

## 2. Construir y levantar

Desde la carpeta raíz:

```bash
docker compose up -d --build
```

Comprueba el estado:

```bash
docker compose ps
```

Debes terminar viendo `db`, `master`, `master2` y `connector` en ejecución; tras unos segundos los healthchecks deben indicar `healthy`.

## 3. Verificar FastAPI + PostgreSQL + dos masters

Ejecuta:

```bash
python scripts/smoke_test.py
```

Debe finalizar con:

```text
Smoke test completado correctamente.
```

Esta prueba valida:
- `/health` en ambas réplicas;
- POST interno y persistencia;
- idempotencia por `idpk`;
- `/history/{id}`;
- DB compartida entre `master` y `master2`;
- paginación;
- filtros `city`, `unit`, `receivedAt`, `metaContent`.

## 4. Probar manualmente la API

Swagger:
- http://localhost:8001/docs
- http://localhost:8002/docs

Historial:

```text
GET http://localhost:8001/history
GET http://localhost:8001/history?page=2&limit=25
GET http://localhost:8001/history?receivedAt=2026-08-26
GET http://localhost:8001/history?city=Santiago&unit=GW
GET http://localhost:8001/history/{id}
```

Otros filtros implementados: `id`, `idpk`, `type`, `validUntil`, `metaContent`, `constraints`, `city`, `demand`, `unit`.

Para `constraints`, envía un objeto JSON codificado como query string, por ejemplo `constraints={}` (el cliente HTTP puede URL-encodearlo).

## 5. Verificar las dos réplicas para balanceo

PowerShell/Windows:

```powershell
curl.exe -i http://localhost:8001/health
curl.exe -i http://localhost:8002/health
```

Busca estos headers:

```text
X-EnergyShark-Instance: master
X-EnergyShark-Instance: master2
```

Esto prueba que cada instancia es individualmente alcanzable desde el host. Más adelante Nginx del EC2 usará precisamente `127.0.0.1:8001` y `127.0.0.1:8002` como upstreams.

## 6. Verificar RabbitMQ real

Mira los logs:

```bash
docker compose logs -f connector
```

Debes ver algo equivalente a:

```text
Conectando a RabbitMQ; cola=observer.53.q
Conectado. Esperando eventos en observer.53.q
```

Cuando llegue un evento real, `connector` lo parsea, lo envía por POST a `master`, y solo hace ACK después de que master lo persiste.

Después revisa:

```text
http://localhost:8001/history?type=demand-set
```

### Advertencia importante
Consumir la cola real hace ACK de los eventos y los elimina de RabbitMQ. En local quedan guardados en tu PostgreSQL local. No ejecutes el connector en dos computadores simultáneamente usando la misma cola.

## 7. Verificar resistencia a caída del broker

Sin tocar el código puedes hacer una prueba controlada:

1. Guarda una copia de `.env`.
2. Cambia temporalmente solo el host de `RABBITMQ_URL` por uno inexistente.
3. Recrea connector:

```bash
docker compose up -d --force-recreate connector
```

4. Observa:

```bash
docker compose logs -f connector
```

Debe **seguir vivo y reintentando**, no terminar permanentemente.
5. Mientras tanto, `http://localhost:8001/history` debe seguir respondiendo porque master y PostgreSQL son independientes.
6. Restaura el `.env` correcto y recrea connector nuevamente.

## 8. Apagar

Conservar DB:

```bash
docker compose down
```

Borrar también todos los datos PostgreSQL locales:

```bash
docker compose down -v
```

## Mapeo rápido al enunciado

### Implementado y comprobable en local
- API de historial y detalle.
- Paginación default 25 y `page`/`limit`.
- Filtros de campos del evento y demandas, incluyendo fechas.
- `receivedAt` generado al recibir el evento.
- `connector` separado y permanente.
- AMQPS con validación TLS activa.
- No se declara/modifica topología RabbitMQ.
- HTTP POST connector→master.
- Reconexión del connector.
- Persistencia PostgreSQL.
- Docker `master`, `master2`, `connector`, `db`.
- Red Docker común.
- HEALTHCHECK en todos los containers.
- Docker Compose.
- Dos réplicas de master individualmente alcanzables desde el host.

### Preparado, pero requiere EC2/dominio para quedar realmente logrado
- Nginx instalado directamente en host EC2.
- Balanceo round-robin Nginx → master/master2.
- Dominio público.
- Let's Encrypt.
- HTTP → HTTPS.
- Renovación automática de Certbot dos veces al día.

Los archivos para esos puntos están en `deploy/`. No es posible obtener un certificado Let's Encrypt válido solo con `localhost`; se termina cuando el dominio público apunte al EC2.

## Seguridad y GitHub

Antes de cualquier commit:

```bash
git status
```

Asegúrate de que **NO** aparezcan:
- `.env`
- archivos `.pem`
- claves privadas

El `.gitignore` ya los excluye.

Además, revisa `ai-docs/prompts/`: el curso exige documentar las consultas a IA, pero las credenciales deben quedar redactadas.
