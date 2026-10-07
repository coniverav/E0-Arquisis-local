# Checklist de Gates E1 — G01 a G08

## Objetivo

Este documento registra la revisión de los gates obligatorios de la Entrega 1 de EnergyShark y la forma en que cada uno puede verificarse antes de la entrega y durante la demo.

La evidencia corresponde al mecanismo de comprobación del requisito. Cuando la condición puede validarse directamente en el sistema desplegado, no es necesario almacenar capturas dentro del repositorio.

---

## Checklist general

| Gate | Requisito | Estado | Evidencia / forma de verificación | Responsable de corrección |
|---|---|---|---|---|
| G01 | Aplicación completa corriendo en cloud/EC2 | [X] | Frontend productivo accesible y API productiva con health `200`; servicios productivos activos en EC2. | Cloud / Backend |
| G02 | Nodo consume `city.{cityId}` y responde ACK/NACK según protocolo | [X] | Logs del `connector` y auditoría de mensajes permiten verificar consumo, ACK, NACK y continuidad del servicio. | Broker / Backend |
| G03 | Ciclo básico autónomo y `negotiation-report` emitido dentro de la ventana | [X] | Ciclos reales procesados sin intervención manual; scheduler y reportes registrados/publicados. | Backend / Scheduler |
| G04 | Backend JSON API y frontend SPA separados | [X] | Repositorios independientes `E0-Arquisis-local` y `Arquisis-G10-Frontend`; FastAPI + React/Vite. | Backend / Frontend |
| G05 | Backend/API y frontend servidos mediante HTTPS | [X] | `https://app.energyshark-g10.tech` y `https://api.energyshark-g10.tech` responden mediante HTTPS. | Cloud |
| G06 | Servicio de autenticación/autorización operativo con JWK estándar | [X] | Login Auth0, JWT válido y rechazo de requests no autorizadas. | Auth / Backend / Frontend |
| G07 | Monitoreo SaaS operativo | [X] | Backend visible en New Relic APM y EC2/contenedores visibles en New Relic Infrastructure. | Observabilidad / Cloud |
| G08 | AWS Budget Alerts configuradas | [X] | Budget Alert activo en la cuenta AWS utilizada por el proyecto. | Cloud / AWS |

---

## G01 — Aplicación completa en cloud/EC2

### Condición

La aplicación debe encontrarse disponible en el ambiente productivo al momento de la revisión.

### Verificación

Frontend:

```text
https://app.energyshark-g10.tech
```

Backend:

```bash
curl -i https://api.energyshark-g10.tech/health
```

Resultado esperado:

```text
HTTP 200
```

En EC2 también se puede comprobar el estado del stack productivo:

```bash
docker compose -f docker-compose.prod.yml ps
```

Los servicios necesarios para la aplicación deben encontrarse operativos.

---

## G02 — Consumo de RabbitMQ y ACK/NACK

### Condición

El nodo de King's Landing debe consumir su cola `city.KLD.q` y responder mensajes según las reglas del protocolo.

### Verificación

Revisar el `connector`:

```bash
docker compose logs connector
```

Se debe poder comprobar:

- conexión al broker
- recepción de mensajes
- procesamiento de mensajes válidos
- ACK de mensajes válidos
- NACK de mensajes inválidos cuando corresponda
- reconexión del connector sin intervención manual

La auditoría de mensajes del sistema también puede utilizarse para revisar duplicados, descartes y NACK.

---

## G03 — Ciclo básico autónomo

### Condición

El sistema debe operar los ciclos sin intervención manual y emitir el `negotiation-report` dentro de la ventana permitida.

El flujo incluye al menos:

- aplicación de `status-statement`
- aplicación de `transfer`
- aplicación de `demand-statement`
- actualización del ledger
- manejo de negociaciones voluntarias cuando corresponda
- emisión del `negotiation-report` dentro del periodo de cierre

### Verificación

Consultar los ciclos desde la API:

```bash
curl https://api.energyshark-g10.tech/cycles
```

También puede revisarse un ciclo desde el frontend y comprobar su detalle y ledger.

Para revisar el scheduler:

```bash
docker compose logs cycle-scheduler
```

---

## G04 — Backend y frontend separados

### Condición

Backend y frontend deben operar como aplicaciones independientes.

### Implementación

Backend:

```text
E0-Arquisis-local
FastAPI
API JSON
```

Frontend:

```text
Arquisis-G10-Frontend
React
Vite
SPA
```

La SPA se comunica con el backend mediante la API pública y ambos se mantienen en repositorios independientes.

---

## G05 — HTTPS

### Condición

Frontend y backend deben estar disponibles mediante HTTPS.

### Verificación

Frontend:

```text
https://app.energyshark-g10.tech
```

API:

```text
https://api.energyshark-g10.tech
```

Ambos deben responder mediante HTTPS válido.

---

## G06 — Autenticación y autorización

### Condición

La aplicación utiliza Auth0 y validación JWT mediante JWKS.

### Verificación

Desde la SPA:

1. Iniciar sesión mediante Auth0.
2. Acceder a las vistas y acciones protegidas.
3. Ejecutar una operación que requiera autenticación, como crear una negociación voluntaria.

Desde la API se debe comprobar:

- token válido: request permitida
- request sin token: rechazada
- token inválido: request rechazada

El backend utiliza `AUTH_JWKS_URL`, `AUTH_AUDIENCE` y `AUTH_ISSUER` para validar los JWT.

---

## G07 — Monitoreo SaaS

### Condición

La aplicación y la infraestructura deben poder observarse mediante New Relic.

### Verificación

En New Relic APM comprobar:

- requests HTTP del backend
- endpoints consultados
- tiempos de respuesta
- errores cuando correspondan

En New Relic Infrastructure comprobar:

- instancia EC2
- estado del host
- contenedores Docker

La revisión puede realizarse directamente desde el panel de New Relic durante la demo.

---

## G08 — AWS Budget Alerts

### Condición

La cuenta AWS utilizada por EnergyShark debe contar con Budget Alerts configuradas.

### Verificación

En AWS:

```text
Billing → Budgets
```

Comprobar que:

- existe un budget asociado al proyecto
- la alerta se encuentra activa
- el umbral configurado es visible

La configuración puede verificarse directamente desde AWS durante la revisión.

---

## Cierre del checklist

- [X] G01 validado
- [X] G02 validado
- [X] G03 validado
- [X] G04 validado
- [X] G05 validado
- [X] G06 validado
- [X] G07 validado
- [X] G08 validado
- [X] Cada gate tiene un responsable de corrección identificado

