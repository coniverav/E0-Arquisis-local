# AGENTS.md — Contexto compartido EnergyShark G10

## 1. Propósito

Este archivo contiene el contexto común del proyecto **EnergyShark - Grupo 10** y debe ser utilizado como referencia por cualquier persona o agente de IA que trabaje en los repositorios de backend, frontend o contratos.

La información compartida aquí complementa la documentación específica de cada repositorio.

---

## 2. Repositorios

### Backend
https://github.com/coniverav/E0-Arquisis-local

Responsabilidades principales:

- API backend de EnergyShark.
- Persistencia en PostgreSQL.
- Consumo y publicación de mensajes mediante RabbitMQ.
- Connector independiente del servidor HTTP.
- Ledger de energía y presupuesto.
- Gestión de ciclos y negociaciones.
- Idempotencia, auditoría, timeouts y reintentos.
- Ejecución mediante Docker Compose.
- Dos instancias de `master` detrás de balanceo.
- Worker independiente `cycle-scheduler`.

Tecnologías principales:

- Python
- FastAPI
- SQLModel / SQLAlchemy
- PostgreSQL
- RabbitMQ
- Docker / Docker Compose
- Nginx

La ciudad asignada al backend es **KLD — King's Landing**.

### Frontend
https://github.com/VicenteIgnacioSotoGonzalez/Arquisis-G10-Frontend

Responsabilidades principales:

- SPA de EnergyShark.
- Login y logout.
- Manejo del token JWT.
- Consumo de la API pública.
- Visualización de ciclos, negociaciones, conectividad y anomalías.
- Creación de negociaciones desde la interfaz.
- Integración con la API desplegada mediante API Gateway.

El frontend **no debe consumir endpoints internos** utilizados por `connector`, workers o comunicación interna del backend.

### Contracts
https://github.com/VicenteIgnacioSotoGonzalez/Arquisis-G10-Contracts

Este repositorio es la **fuente de verdad de los contratos compartidos** entre frontend, backend y servicios externos.

Debe contener, al menos:

- Schemas de los mensajes de la mecánica.
- Definiciones del protocolo utilizado con RabbitMQ.
- OpenAPI de la API pública del backend.
- Este archivo `AGENTS.md` de contexto compartido.

Si cambia la forma de un mensaje o de un endpoint público, el cambio debe reflejarse primero o simultáneamente en este repositorio.

---

## 3. Arquitectura general

Flujo principal de mensajes:

```text
Central / RabbitMQ
        |
        v
    connector
        |
        v
   Backend FastAPI
        |
        v
    PostgreSQL
```

Para mensajes salientes:

```text
Backend
   |
   v
Outbound persistido
   |
   v
connector
   |
   v
RabbitMQ / Central
```

Flujo de frontend:

```text
Usuario
   |
   v
Frontend SPA
   |
   | JWT
   v
API Gateway
   |
   v
Backend
   |
   v
PostgreSQL
```

El backend mantiene separadas las responsabilidades de:

- API HTTP.
- Comunicación con RabbitMQ.
- Persistencia.
- Procesamiento temporal mediante workers.

No se deben implementar timers largos o esperas bloqueantes dentro de requests HTTP.

---

## 4. Contratos de mensajes

El protocolo de mensajes utiliza un envelope común con los campos:

```json
{
  "idpk": "uuid",
  "msgId": "uuid",
  "type": "message-type",
  "timestamp": "ISO-8601"
}
```

Reglas importantes:

- `msgId` identifica un mensaje concreto.
- `idpk` es la llave de idempotencia de una operación.
- `idpk` y `msgId` deben ser distintos.
- Los mensajes enviados por una ciudad incluyen `cityId`.
- Los mensajes emitidos por la central utilizan `sender: "central"`.
- Los mensajes ligados a un ciclo incluyen `cycleId`.
- El contenido específico del mensaje viaja dentro de `data`.
- Una respuesta utiliza un `msgId` nuevo y referencia el mensaje anterior mediante campos como `data.target` o `data.becauseOf`.
- Los schemas versionados del repositorio Contracts tienen prioridad sobre ejemplos informales.

No inventar campos nuevos ni cambiar nombres de campos sin actualizar el contrato compartido.

---

## 5. Negociaciones

Estados utilizados por el backend:

```text
PENDING_PUBLICATION
PROPOSED
ACKNOWLEDGED
CONFIRMED
PAID
REJECTED
TIMEOUT
```

Principios importantes:

- Un `ACK` confirma recepción, no aceptación de una negociación.
- `give` y `take` son confirmaciones de negociación.
- Los efectos de energía y presupuesto se registran de forma trazable.
- Una misma operación sensible no puede modificar el ledger dos veces.
- Los timeouts se basan en deadlines persistidos en PostgreSQL.
- El worker independiente procesa deadlines vencidos.
- Los reintentos deben preservar la idempotencia.
- No usar `sleep(30)` ni timers en memoria para controlar una negociación.

---

## 6. Ledger y persistencia

El backend mantiene:

- Un registro histórico de operaciones.
- Un estado materializado del ciclo para consultas rápidas.

El ledger debe permitir explicar cómo se obtuvo el estado de un ciclo.

Principios:

- Los cambios de energía y presupuesto deben ser persistentes.
- Las operaciones sensibles deben ser idempotentes.
- Los duplicados no deben volver a modificar el ledger.
- Los efectos relacionados deben ejecutarse dentro de una transacción cuando corresponda.
- Un timeout por sí mismo no debe modificar energía ni presupuesto.
- El presupuesto puede continuar entre ciclos.
- La energía no se arrastra al ciclo siguiente.

---

## 7. Autenticación y API pública

El frontend consume únicamente la API pública.

Flujo esperado:

```text
Frontend
   |
   | login
   v
Proveedor de autenticación
   |
   | JWT
   v
Frontend
   |
   | Authorization: Bearer <token>
   v
API Gateway
   |
   | authorizer
   v
Backend
```

Reglas:

- No almacenar secretos en el frontend.
- No hardcodear tokens.
- La URL pública de API debe configurarse por ambiente.
- CORS debe permitir únicamente los orígenes esperados.
- Los endpoints internos del backend no forman parte del contrato consumido por el frontend.

La definición OpenAPI versionada en Contracts es la referencia para los endpoints públicos.

---

## 7.1 Infraestructura y observabilidad de E1

El despliegue productivo de EnergyShark utiliza:

```text
Frontend
  ↓
Amazon S3
  ↓
CloudFront
  ↓
API Gateway
  ↓
https://api.energyshark-g10.tech
  ↓
EC2
  ↓
Docker Compose
  ↓
Backend / PostgreSQL / RabbitMQ connector
```

### Backend

El backend productivo se despliega en EC2 utilizando imágenes versionadas almacenadas en Amazon ECR.

Se mantiene separación entre:

```text
docker-compose.yml
→ desarrollo/local

docker-compose.prod.yml
→ producción
→ utiliza imágenes desde ECR
```

Las imágenes propias se versionan utilizando el SHA del commit desplegado:

```text
api-<sha>
connector-<sha>
```

Los servicios principales de producción son:

```text
db
migrate
master
master2
connector
cycle-scheduler
newrelic-infra
```

### Frontend

El frontend es una SPA estática desplegada mediante:

```text
Amazon S3
  ↓
CloudFront
  ↓
HTTPS
```

El frontend productivo consume la API pública:

```text
https://api.energyshark-g10.tech
```

El frontend no debe consumir directamente endpoints internos ni depender de la IP de EC2.

### Autenticación

Auth0 entrega JWT al frontend.

Las operaciones protegidas pasan por el JWT Authorizer de API Gateway antes de llegar al backend.

El audience compartido es:

```text
https://arquisis-e1-api/
```

### Observabilidad

La aplicación utiliza New Relic para APM y monitoreo de infraestructura.

```text
master / master2
    ↓
New Relic APM

EC2 / Docker
    ↓
New Relic Infrastructure
```

El servicio APM se identifica como:

```text
EnergyShark Backend
```

El host de infraestructura se identifica como:

```text
EnergyShark EC2
```

Las credenciales de New Relic se entregan mediante variables de entorno y nunca deben versionarse.


## 8. Responsabilidades por repositorio

### Backend

Puede modificar:

- lógica de negocio;
- persistencia;
- workers;
- endpoints;
- integración RabbitMQ;
- implementación de idempotencia;
- reglas de negociación.

No debe cambiar unilateralmente la forma de una API o mensaje compartido sin actualizar Contracts.

### Frontend

Puede modificar:

- componentes;
- vistas;
- navegación;
- estado de UI;
- autenticación del cliente;
- consumo de API.

No debe asumir campos que no estén definidos en OpenAPI o en contratos compartidos.

### Contracts

Debe mantenerse independiente de la implementación.

Su función es describir:

- qué mensajes existen;
- qué campos reciben;
- qué campos son obligatorios;
- qué respuestas entrega la API;
- qué endpoints públicos existen.

No debe contener secretos ni lógica específica de despliegue.

---

## 9. Regla de precedencia

Al resolver dudas, utilizar este orden:

1. Enunciado oficial de la entrega.
2. Contratos versionados en `Arquisis-G10-Contracts`.
3. ADRs y documentación arquitectónica.
4. Implementación actual.
5. Este `AGENTS.md`.
6. Suposiciones del desarrollador o agente.

Si existe una inconsistencia, no corregirla silenciosamente. Debe identificarse y resolverse explícitamente.

---

## 10. Flujo recomendado para cambios compartidos

Cuando un cambio afecta frontend y backend:

```text
1. Definir o modificar el contrato.
2. Actualizar Contracts.
3. Adaptar backend.
4. Adaptar frontend.
5. Ejecutar tests.
6. Verificar integración end-to-end.
7. Actualizar documentación si corresponde.
```

Ejemplos de cambios que requieren actualizar Contracts:

- nuevo endpoint público;
- cambio de path;
- cambio de request o response;
- nuevo tipo de mensaje;
- campo obligatorio nuevo;
- cambio de nombre o tipo de un campo;
- cambio de códigos de error públicos.

Cambios internos que normalmente no requieren modificar Contracts:

- refactor interno;
- optimización SQL;
- cambio de estructura de servicios;
- worker interno;
- cambio de implementación sin alterar interfaces externas.

---

## 11. Convenciones para agentes de IA

Antes de proponer cambios:

1. Leer este archivo.
2. Leer la documentación local del repositorio en el que se está trabajando.
3. Revisar los contratos correspondientes.
4. Revisar la implementación existente antes de crear archivos o abstracciones nuevas.

Reglas:

- No inventar endpoints, campos o estados.
- No reemplazar contratos existentes por supuestos.
- Preferir reutilizar servicios y patrones ya presentes.
- Mantener compatibilidad con Docker Compose.
- Mantener idempotencia en operaciones sensibles.
- No introducir secretos, credenciales o `.env` reales.
- No modificar infraestructura o contratos fuera del alcance de la tarea sin indicarlo.
- Mantener separados frontend, backend y contracts.
- Ante un cambio cross-repo, indicar qué repositorios deben actualizarse.

---

## 12. Seguridad y secretos

Nunca versionar:

```text
.env
*.pem
private keys
JWT reales
passwords
RABBITMQ_URL con credenciales reales
credenciales de PostgreSQL
AWS access keys
tokens de proveedores externos
```

Usar `.env.example` con valores ficticios para documentar configuración.

---

## 13. Ejecución local del backend

Flujo habitual:

```bash
docker compose down --remove-orphans
docker compose up -d --build
docker compose ps -a
```

Tests:

```bash
docker compose exec master python -m pytest -v
```

Los servicios principales incluyen:

```text
db
migrate
master
master2
connector
cycle-scheduler
```

---

## 14. Definición de terminado para integración

Un cambio compartido está terminado cuando:

- el contrato está versionado;
- backend y frontend respetan el mismo contrato;
- los tests relevantes pasan;
- no se rompe idempotencia;
- los flujos principales funcionan end-to-end;
- autenticación y autorización funcionan cuando corresponda;
- no se exponen secretos;
- la documentación refleja el comportamiento real.

---

## 15. Referencias de repositorios

- Backend: https://github.com/coniverav/E0-Arquisis-local
- Frontend: https://github.com/VicenteIgnacioSotoGonzalez/Arquisis-G10-Frontend
- Contracts: https://github.com/VicenteIgnacioSotoGonzalez/Arquisis-G10-Contracts
