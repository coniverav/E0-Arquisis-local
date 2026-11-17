# Objetivo

La E3 busca conservar el nodo EnergyShark completo de E1/E2 mientras reemplaza la coordinación distribuida de swaps por **sagas orquestadas en AWS Step Functions Standard** y vuelve durables las publicaciones e inicios críticos mediante un **outbox transaccional**. La entrega también debe permitir que un tercero opere, observe y reconstruya el sistema mediante alertas, SSE autenticado, infraestructura como código y evidencia de seguridad.

El protocolo v3, Wallet, las dos colas y los ciclos de 20 minutos no cambian. La migración debe ocurrir sin suspender el mercado, perder reportes ni reinterpretar hechos económicos ya persistidos.

# Alcance

* Dos máquinas Step Functions Standard, una para vendedor y otra para comprador, con tareas Lambda, esperas mediante task token, timeouts, reintentos, reconciliación y compensaciones.
* Una ejecución por operación, identificada por el `idpk` que inicia el flujo, con correlación durable por `idpk`, `swapId`, rol, ARN de ejecución y task token.
* Outbox transaccional para `negotiation-report`, mensajes de oferta e inicio de sagas; relay independiente con publisher confirms o confirmación de `StartExecution` y entrega al menos una vez.
* Conservación del consumo RabbitMQ en EC2, de la validación AMQP, de la idempotencia v3 y de la aplicación de eventos Wallet al ledger local.
* Feed administrativo mediante SSE autenticado, con identificadores ordenados y recuperación por `Last-Event-ID`, sin depender de polling periódico del navegador.
* Cinco alertas propias del nodo: Wallet lenta, consumidor detenido/atrasado, diferencia ledger–Wallet, saga atascada/fallida y outbox atrasado; cada una con umbral, detección, canal y runbook.
* Infraestructura declarada con Terraform o CDK —y SAM/Serverless si se justifica para la parte serverless—, nueva infraestructura desplegada desde código e IAM de mínimo privilegio.
* Prowler antes y después de las correcciones, con al menos tres hallazgos de severidad media o superior reparados y explicados.
* Regresión de E1/E2, contratos v3.0.2, documentación, evidencias y preparación de la demo.

# Fuera de alcance

* Cambiar los schemas, mensajes, endpoints o mecánica del protocolo v3.
* Reemplazar Wallet como autoridad económica o trasladar el ledger local a Step Functions.
* Consumir RabbitMQ desde Lambda; los consumidores existentes permanecen en EC2.
* Interpretar HTTP `202` como liquidación o liberar reservas sólo por timeout local.
* Garantizar exactamente una publicación física. El outbox garantiza intención durable y entrega al menos una vez; la operación se deduplica por sus identificadores.
* Usar Step Functions Express para los swaps, porque el flujo requiere callbacks y espera durable.
* Importar o destruir la infraestructura productiva actual sólo para declararla en IaC.
* Introducir nuevas estrategias de mercado o cambiar el experimento de span fuera de los ajustes necesarios para conservar AD6.

# Arquitectura objetivo

```text
RabbitMQ city/market
        │
        ▼
Connector EC2 ──► FastAPI/PostgreSQL ──► ledger y auditoría
                         │
                         ├── transacción negocio + outbox
                         │                         │
                         │                         ▼
                         │                 relay independiente
                         │                  ├── RabbitMQ + confirm
                         │                  └── StartExecution
                         │                              │
                         ▼                              ▼
                callback de evento            Step Functions Standard
                Wallet + task token             seller / buyer
                         ▲                              │
                         └──────────── Lambda ◄────────┘

PostgreSQL/auditoría/outbox ──► SSE autenticado ──► panel React
CloudWatch + New Relic ───────► alertas y runbook
Terraform/CDK ────────────────► recursos AWS e IAM
```

## Fronteras

* **EC2/Connector:** consume las colas, valida identidad y protocolo, persiste el mensaje, aplica eventos Wallet idempotentemente y reanuda la ejecución que espera ese hecho.
* **PostgreSQL:** conserva negocio, ledger, deduplicación, outbox y correlaciones. El task token se persiste antes de invocar Wallet.
* **Step Functions/Lambda:** coordinan secuencia, HTTP, esperas, reintentos y compensaciones; no redefinen estrategia ni aplican el ledger por una ruta paralela.
* **Wallet:** mantiene autoridad sobre activos, retenciones y resultado del swap.
* **Frontend/SSE:** observa eventos autorizados; no recibe secretos, `swapToken`, API keys ni payloads internos sin filtrar.

# Supuestos y dependencias

* El protocolo oficial sigue siendo v3.0.2 y las reglas de E1/E2 no derogadas continúan vigentes.
* El mercado opera 72 ciclos diarios y un `negotiation-report` omitido mantiene su costo de 50.000 CR.
* Las escrituras Wallet pueden responder `202`, perder la respuesta o entregar eventos tardíos y desordenados. Los reintentos conservan la misma `Idempotency-Key`.
* Step Functions Standard cobra por transición y Lambda por solicitudes/duración; las alertas de presupuesto deben cubrir ambos servicios.
* Los nombres de ejecución basados en `idpk` complementan la deduplicación persistente. `ExecutionAlreadyExists` se trata como estado conocido, nunca como permiso para crear otra identidad.
* Los ADR de sagas y outbox deben preceder a la implementación. Sus mediciones se completarán con evidencia real durante la entrega.
* Las fechas del plan del 16 de noviembre al 13 de diciembre de 2026 son internas y tentativas; el material disponible no fija una fecha oficial.

# Decisiones tomadas y pendientes

## Decisiones tomadas

* Dos máquinas Standard separadas por rol, manteniendo ledger y consumidores RabbitMQ fuera de la saga.
* Outbox PostgreSQL con relay por polling, bloqueo transaccional y entrega al menos una vez.
* Identidad estable para mensajes, intentos Wallet y ejecuciones; ningún reintento inventa `idpk`, `msgId` o `Idempotency-Key`.
* Migraciones aditivas y despliegue sin pérdida de la base E2.

## Decisiones pendientes antes de implementación o despliegue

* Elegir Terraform o CDK y definir estado remoto, módulos, ambientes y estrategia de adopción sin destruir producción.
* Precisar el esquema de saga, callbacks, leases y correlaciones, incluyendo cifrado/retención de task tokens.
* Definir el intervalo/lote del relay y el orden por entidad sin exceder el objetivo de recuperación de 60 segundos.
* Elegir la ruta y autenticación SSE compatibles con API Gateway/navegador, incluyendo expiración de sesión, CORS y replay.
* Fijar umbrales de las cinco alertas a partir de telemetría real de E2 y probar el canal de notificación.
* Definir el corte que desactiva la coordinación E2 de swaps y habilita sagas sin procesar una operación por ambos caminos.
* Seleccionar tres hallazgos Prowler MEDIUM+ cuya remediación sea segura y demostrable.

# Requisitos esenciales

* **G01 — Nube:** aplicación completa y disponible en cloud durante la revisión.
* **G02 — Continuidad E1/E2:** ambas colas, protocolo, ciclos, reportes, frontend/backend HTTPS, autenticación, APM y budgets —incluidos Step Functions y Lambda— operativos.
* **G03 — Saga:** al menos un swap real `SETTLED` en producción, como comprador o vendedor, coordinado por Step Functions Standard y con Execution History visible.
* **G04 — Outbox:** `negotiation-report`, ofertas e inicios de saga salen desde el outbox y se recuperan ante fallas.
* **G05 — Prowler:** reporte inicial, tres hallazgos MEDIUM+ corregidos y reporte posterior.

La falta de cualquiera de estos cinco gates limita la nota máxima según el enunciado.

# Requisitos funcionales y arquitectónicos

* **RF01 — Feed SSE:** mostrar eventos de mercado, Wallet, saga y alertas mediante un stream autenticado. Al reconectar con `Last-Event-ID`, entregar sólo los eventos posteriores sin pérdida ni duplicación visible.
* **AD7 — Saga orquestada:** cada swap termina liquidado, rechazado o compensado; ninguna ejecución queda abierta 60 segundos después de `closesAt`; no quedan reservas locales retenidas al cierre; los reenvíos no crean otra ejecución.
* **AD8 — Publicación confiable:** el cambio y la intención outbox comparten transacción; se conserva el envelope original; tras recuperar broker/relay, la edad de la fila pendiente más antigua vuelve a cero en hasta 60 segundos.
* **RNF01 — Observabilidad:** cinco alertas con condición, umbral, detección objetivo, canal probado y logs correlacionados por `cycleId`, `swapId` e `idpk`.
* **RNF02 — IaC e IAM:** nueva infraestructura desplegada desde código; stack previo recreable en una cuenta limpia; permisos mínimos y secretos fuera de Git.
* **RNF03 — Seguridad:** Prowler antes/después y evidencia específica de tres reparaciones MEDIUM+.

# Reglas de saga y recuperación

## Vendedor

1. Reservar energía mediante la garantía transaccional de AD4.
2. Registrar ejecución y task token antes del POST a Wallet.
3. Proponer con la misma `Idempotency-Key`; esperar `swap-proposed` o `swap-rejected`.
4. Publicar `bid-accepted` sólo tras `swap-proposed`.
5. Esperar `swap-settled`, `swap-expired` o `swap-cancelled`; si falta el evento, consultar Wallet.
6. Cancelar cuando corresponda y compensar liberando reserva y actualizando `remaining` por outbox.

## Comprador

1. Iniciar una ejecución por `bid-accepted` válido.
2. Verificar firma ES256, claims, activos, `memo`, `expiresAt` y tiempo restante del ciclo.
3. Aceptar con llave estable; esperar evento terminal.
4. Reconciliar por `swapId` si no llega el callback y terminar según la autoridad Wallet.

## Política HTTP

* `202`: continuar esperando un evento; no liquidar.
* `200`: operación ya conocida; continuar según estado devuelto.
* `429`: respetar `Retry-After` y reutilizar la llave.
* `502/503/504` o timeout: backoff con la misma llave y dentro del deadline.
* `422 IDEMPOTENCY_MISMATCH`, `401` o `404`: error no reintentable salvo evidencia contractual distinta.

# Documentación y proceso

* **RDOC01:** post-mortems de AD4 y AD6 con evidencia real de E2 y propuestas para E3.
* **RDOC02:** ADR7 previo al código, con alternativas, fronteras, costos y objetivos medidos.
* **RDOC03:** ADR8 previo al código, con alcance de mensajes, relay y garantías.
* **RDOC04:** UML final y runbook de las cinco alertas.
* Mantener milestones, AI logs reales y PRs revisadas según la política del equipo. No simular fechas, aprobaciones, resultados o uso de IA.

# Criterios de aceptación medibles

1. **Continuidad:** frontend y API responden por HTTPS; ambas colas reciben mensajes; un ciclo procesa `status-statement`, Wallet/cierre y publica `negotiation-report` en ventana.
2. **Seller:** un bid válido produce una sola reserva y una sola ejecución; `bid-accepted` ocurre después de `swap-proposed`; cada resultado termina o compensa el `HOLD`.
3. **Buyer:** un `bid-accepted` válido inicia una sola ejecución, valida token/plazos y acepta con llave estable; un token inválido no llama a Wallet.
4. **Deduplicación:** reentregar el mensaje iniciador con el mismo `idpk` no crea otra ejecución, swap, reserva ni efecto económico.
5. **Carrera de callback:** un evento recibido inmediatamente después del POST encuentra un task token ya persistido y reanuda exactamente la ejecución correlacionada.
6. **HTTP incierto:** pérdida de respuesta, `429` y `5xx` reutilizan la misma llave; `202` no aparece como liquidación.
7. **Estado terminal AD7:** cero ejecuciones abiertas 60 segundos después de `closesAt` y cero reservas locales retenidas al cierre, o desviación explícitamente medida y explicada.
8. **Atomicidad AD8:** una caída después del commit deja una fila outbox recuperable; no existe cambio cubierto sin su intención ni intención sin el estado correspondiente.
9. **Recuperación AD8:** tras restaurar relay/broker, backlog y edad pendiente vuelven a cero en hasta 60 segundos, preservando `idpk` y `msgId`.
10. **SSE:** usuario autenticado recibe eventos, reconecta con `Last-Event-ID` y recupera el tramo faltante; usuario no autorizado no accede y no se filtran secretos.
11. **Alertas:** las cinco condiciones generan notificación en el canal configurado y cada alerta puede diagnosticarse con su runbook.
12. **IaC/seguridad:** plan/synth reproducible, infraestructura serverless desplegada desde código, IAM sin comodines amplios y Prowler demuestra tres reparaciones MEDIUM+.
13. **Regresión:** suites Master, Connector, Frontend y Contracts permanecen verdes; un swap productivo `SETTLED` puede trazarse desde broker/outbox hasta Execution History, Wallet y ledger.
