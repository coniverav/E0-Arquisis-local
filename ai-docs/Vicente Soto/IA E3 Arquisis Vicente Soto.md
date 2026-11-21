# AI Log - Vicente Soto

# ADR7, ADR8 y post-mortems de AD4/AD6

### Prompt relevante

> "Me ayudas a hacer estas tarjetas? Estoy ahora en el contexto de Enunciado E3 Arquisis.pdf, ya definimos los ADRs nuevos que son estos. Hay que hacer los postmortems como los hicimos en la entrega pasada pero con los ADRs 4,5 y 6."

### Resumen de la respuesta y utilidad

Se revisaron el enunciado E3, los ADR de E2, sus revisiones retrospectivas, los post-mortems anteriores y la evidencia disponible del experimento de span y de concurrencia de bids. El enunciado aclaró que RDOC01 exige post-mortems de AD4 y AD6; AD5 continúa vigente, pero no requiere un post-mortem separado en E3.

Se documentó ADR7 proponiendo dos máquinas AWS Step Functions Standard, una por rol, manteniendo los consumidores RabbitMQ y la aplicación idempotente del ledger en EC2/PostgreSQL. El ADR distingue responsabilidades, uso de task tokens, ejecución identificada por `idpk`, política de reintentos, costo estimado y objetivos medibles. ADR8 define un outbox transaccional con relay por polling, `SELECT FOR UPDATE SKIP LOCKED`, publisher confirms y entrega al menos una vez. También separa mensajes incluidos y excluidos sin prometer exactamente una publicación física.

Los post-mortems contrastan las decisiones de E2 con evidencia real y local. AD4 registra el ensayo 80+80 KWH con separación cercana a 40 ms y limita sus conclusiones frente a Wallet Central; también distingue la pérdida de `reason` observada en rechazos de una doble reserva no demostrada. AD6 usa los ciclos del experimento de 20, 60 y 120 segundos, la ausencia de compras comparables y la capacidad económica negativa observada posteriormente, sin declarar un span ganador.

Después de una revisión de estilo, ambos post-mortems se ajustaron al formato previo: decisión y resultado, evidencia, tradeoff/divergencias y cierre. Los documentos quedaron en la PR Backend **#96**, sin código funcional, infraestructura ni secretos. Los commits de la rama separan ADR7, ADR8, AD4 y AD6; una corrección posterior simplificó los encabezados solicitados.

---

# Continuidad E1/E2, spec y milestones E3

### Prompt relevante

> "Ahora en teoría tengo que hacer esto, la primera tarjeta tengo la duda según enunciado, la segunda sí o sí, te puedo pasar la estructura de E0-Arquisis-local/docs/milestones/milestones_e2.md pero la información está en Planificación E3 Arquisis.pdf para que sepas la info. Esto hagámoslo en otra rama y hacemos otra PR."

### Resumen de la respuesta y utilidad

Se contrastó el enunciado con la planificación interna. La auditoría E3-001 no entrega puntos documentales por sí sola, pero es necesaria para demostrar G01/G02: la aplicación E1/E2 debe seguir funcionando en cloud con ambas colas, protocolo, ciclos, reportes, HTTPS, autenticación, APM y budgets que incorporen Lambda y Step Functions.

La revisión separó evidencia pública, respaldo de código y validación productiva pendiente. El 17 de noviembre de 2026 se hicieron comprobaciones públicas de solo lectura: `https://api.energyshark-g10.tech/health` respondió HTTP 200 con TLS válido, instancia `master` y acceso a PostgreSQL; el frontend público también respondió HTTP 200 por HTTPS. No se utilizaron credenciales ni se accedió a RabbitMQ, Wallet, Auth0, New Relic o AWS Billing, por lo que esos componentes no se declararon operativos solamente por aparecer en Compose o documentación.

La matriz de continuidad identifica riesgos de migración: pérdida de reportes, doble coordinación entre workers E2 y sagas E3, callback antes de persistir el token, outbox insuficiente para `StartExecution`, autenticación SSE, evidencia histórica desactualizada e infraestructura aún no reproducible. G01/G02 quedan como línea base identificada, no cerrados.

La spec E3 define objetivo, alcance, arquitectura, fronteras, supuestos, decisiones pendientes, cinco gates y criterios medibles para sagas, outbox, SSE, alertas, IaC y Prowler. `milestones_e3.md` resume las 98 tarjetas de la planificación en cinco fases, manteniendo las fechas como propuesta interna y no como plazo oficial del curso.

Los documentos quedaron en la PR Backend **#97**, con tres commits separados para auditoría, spec y milestones. La validación documental incluyó referencias locales y `git diff --check`; no se ejecutaron pruebas funcionales porque no se modificó código.

---

# Inventario AWS heredado de E2

### Prompt relevante

> "En el repositorio backend de EnergyShark, crea una nueva rama `docs/e3-010-inventario-aws` a partir de la versión más reciente de `develop`. En esa rama, crea `docs/E3-010-inventario-aws.md` con un resumen breve del inventario AWS de E2."

### Resumen de la respuesta y utilidad

Se preparó un inventario conciso de la infraestructura heredada: EC2 y EBS, ECR, API Gateway, S3, CloudFront, IAM, CloudWatch, Budgets y el estado todavía no desplegado de Step Functions y Lambda. El documento diferencia el inventario entregado de una verificación operativa actual y deja New Relic expresamente pendiente de comprobación.

También se registró la estimación preliminar de costos para E3 bajo el supuesto de 72 ciclos diarios y dos swaps por ciclo, junto con los elementos excluidos del cálculo. El volumen EBS sin cifrar y el acceso público a S3 quedaron identificados como posibles hallazgos para validar posteriormente con Prowler, sin presentar todavía una remediación.

El cambio quedó en la PR Backend **#99**, commit `f885f26`. Solo se añadió documentación; no se ejecutaron comandos AWS ni se modificó infraestructura.

---

# Baseline inicial de Prowler

### Prompt relevante

> "Documenta la tarjeta E3-011 en `docs/E3-011-prowler-baseline.md`, con un Markdown breve y conciso. Incluye el escaneo inicial realizado el 17/11/2026 con Prowler 5.50.0, los resultados, la evidencia generada y tres candidatos de remediación."

### Resumen de la respuesta y utilidad

Se documentó la línea base informada para Prowler 5.50.0: 671 checks ejecutados, 258 hallazgos fallidos y 177 aprobados, con 4 críticos, 33 altos, 116 medios y 105 bajos. La ubicación de los reportes CSV, HTML y JSON OCSF se dejó como referencia local, sin incorporar esos archivos al repositorio para evitar exponer identificadores o detalles sensibles de AWS.

El documento propone revisar el cifrado EBS predeterminado, MFA para el usuario IAM `grupo10` y el acceso público del bucket S3. Se aclaró que son candidatos y que todavía no se aplicaron correcciones. No se volvió a ejecutar Prowler ni se modificó infraestructura.

El cambio quedó en la PR Backend **#100**, commit `7b42c7c`, limitado a un Markdown de 33 líneas.

---

# Baseline local de pruebas E2

### Prompt relevante

> "Documenta E3-012 — Baseline de pruebas E2 en `docs/E3-012-baseline-tests.md`. El documento debe ser muy breve, con una tabla de resultados para contratos, frontend, backend y connector, además de los comandos esenciales y las condiciones de aislamiento."

### Resumen de la respuesta y utilidad

Se consolidó la evidencia local informada para los tres repositorios: Contracts 15/15, Frontend 30/30 con lint y build correctos, Backend 525/525 más 3 subtests y Connector 74/74 más 12 subtests. El total registrado es de 644 tests y 15 subtests aprobados. Se fijaron como referencias `bbd8eae` para Contracts, `e9b7424` para Frontend y `22e9edd` para el `develop` del Backend.

El documento incluye los comandos esenciales de reproducción y explica que el backend usó PostgreSQL temporal aislado, mientras las pruebas del Connector utilizaron RabbitMQ ficticio. También registra la advertencia no bloqueante de Vite y aclara que el resultado constituye evidencia local, no una validación de producción.

El cambio quedó en la PR Backend **#101**, commit `0bb6d10`. No se modificó código funcional ni se incluyeron credenciales.

---

# Estructura base de infraestructura como código

### Prompt relevante

> "Implementa E3-014 — Definir estructura IaC en `E0-Arquisis-local`. Usa AWS CDK con Python. Inicializa una estructura mínima en `infra/cdk/`, separando infraestructura existente, sagas (Step Functions/Lambda) y monitoreo."

### Resumen de la respuesta y utilidad

Se creó una aplicación AWS CDK v2 con Python y tres límites de stack independientes: infraestructura E2 existente, futuras sagas con Step Functions/Lambda y monitoreo. Los stacks quedaron deliberadamente vacíos para que esta base no cree, importe ni modifique recursos AWS antes de revisar el estado de CloudFormation y definir una estrategia segura de adopción.

La configuración admite `dev`, `staging` y `prod`, resuelve cuenta y región desde el entorno y activa protección contra eliminación en producción. No se incorporaron identificadores, credenciales ni secretos; la documentación establece que los valores futuros deben referenciar Secrets Manager o SSM Parameter Store. También se registró una estrategia incremental basada en tests, síntesis, `cdk diff`, revisión, importación explícita y aprobación manual para producción.

La validación se realizó en un entorno virtual temporal con `aws-cdk-lib` 2.277.0. Pasaron 2/2 pruebas y se sintetizaron tres plantillas sin recursos AWS. No se ejecutó `cdk deploy` ni se accedió a una cuenta AWS.

La implementación quedó en la PR Backend **#102**, commit `180d1e0`, limitada a `infra/cdk/`.

---

# Persistencia y diseño de sagas de compra y venta

### Prompt relevante

> "Implementa E3-015, E3-016, E3-017 y E3-018 según el enunciado E3 y AD7. Hay PRs pendientes: revisa las ramas existentes y crea una nueva rama desde la de E3-014, conservando su estructura CDK. Implementa persistencia durable y correlación de task tokens, la migración Alembic aditiva y las máquinas Standard del vendedor y comprador."

### Resumen de la respuesta y utilidad

Se comprobó que AD7 ya estaba documentado en la PR Backend #96, aunque todavía no era ancestro de la rama de E3-014. La rama de implementación se creó directamente sobre `feat/e3-014-cdk-base` y la PR dejó explícita su dependencia documental: AD7 debe integrarse antes, sin duplicar su commit ni mergear anticipadamente las PR pendientes.

Para la persistencia se añadieron ejecuciones de saga y task tokens correlacionables por `idpk`, `swapId`, rol, ARN de ejecución y timeout. El servicio conserva reintentos idempotentes, detecta correlaciones ambiguas o incompatibles, permite completar cada callback una sola vez y expira esperas sin inferir un resultado económico. El token se guarda porque Step Functions lo necesita para completar el callback, pero se protege mediante exclusión de serialización y un hash único para búsquedas y deduplicación.

La migración Alembic es aditiva: crea únicamente las tablas, restricciones e índices de sagas. Una prueba parte del último head de E2, inserta un swap sintético, migra a E3 y comprueba que el registro anterior se conserva. No se modificaron ni eliminaron tablas históricas de E2.

También se versionaron las definiciones ASL Standard para ambos roles. La saga seller reserva energía, propone el swap con callback, reconcilia resultados inciertos y compensa solamente cuando Wallet confirma rechazo, expiración o cancelación. La saga buyer valida el JWT, comprueba el plazo, acepta con una llave de idempotencia estable, espera callbacks y reconcilia timeouts. Ninguna definición transporta `swapToken`; las futuras Lambdas deberán recuperarlo desde PostgreSQL.

La suite completa de Master terminó con **530 tests y 3 subtests aprobados**. Las pruebas enfocadas de persistencia y migración aprobaron 5/5; CDK/ASL aprobó 11/11; `alembic upgrade head`, `alembic check` y `git diff --check` también resultaron correctos. La prueba de migración utilizó PostgreSQL temporal aislado. No se desplegaron recursos AWS ni se accedió a servicios productivos.

Los cambios quedaron en la PR Backend **#103**, apilada sobre la rama de E3-014 y no sobre `develop`. La creación efectiva de State Machines, Lambdas, permisos IAM y callbacks del Connector se dejó para las tareas posteriores, evitando presentar el diseño ASL como infraestructura ya desplegada.

---

# Infraestructura serverless e IAM de las sagas

### Prompt relevante

> "Implementa E3-019, E3-020 y E3-021 según el enunciado E3, AD7 y RNF02. Declara en CDK las dos máquinas Step Functions Standard, las Lambdas necesarias con configuración por ambiente y empaquetado reproducible, e IAM de mínimo privilegio. No despliegues AWS y abre una PR hacia la rama de E3-015–018."

### Resumen de la respuesta y utilidad

Se creó una rama apilada sobre E3-015–018 para reutilizar las definiciones ASL y la estructura CDK sin duplicar cambios de las PR pendientes. El stack de sagas ahora declara dos máquinas Step Functions Standard, una seller y otra buyer, con timeout total parametrizable, logs de nivel `ALL` sin incluir payloads, versiones CloudFormation y alias estable `live`.

Se declararon 13 Lambdas separadas por responsabilidad, usando Python 3.12 y un artefacto común sin dependencias externas cuyo hash determina el paquete CDK. Memoria, timeout y retención de logs se configuran por ambiente. Los handlers quedan deliberadamente en modo seguro: registran solamente identificadores de correlación y fallan explícitamente hasta que las tareas posteriores implementen Wallet y el orden token-antes-HTTP; no simulan lógica de negocio inexistente.

Los secretos se reciben como ARN mediante parámetros CloudFormation `NoEcho`. Cada Lambda tiene un rol propio y solo puede leer los secretos de base, Wallet o broker que necesita. Las máquinas solo pueden invocar sus funciones exactas. El rol e instance profile para EC2 limita `StartExecution` a los alias seller y buyer. Los únicos recursos comodín corresponden a `SendTaskSuccess/Failure` y a las APIs de entrega de CloudWatch Logs, que AWS no permite restringir por ARN.

La validación aprobó **18/18 pruebas** de CDK, ASL, configuración, IAM y protección del logging. También pasaron la compilación Python, `git diff --check` y `cdk synth EnergySharkDev-Sagas`. La síntesis se ejecutó localmente sin credenciales ni despliegue; solo se observó una advertencia no bloqueante porque la instalación local de `npx` disponible utiliza Node 20, ya fuera de soporte para la CLI actual.

La implementación quedó en la PR Backend **#104**, commit `824ae43`, dirigida a `feat/e3-015-018-saga-foundation`. No se ejecutó `cdk deploy`, no se modificó producción y la asociación efectiva del instance profile a EC2 continúa pendiente del despliegue IaC.

---

# Cliente Lambda Wallet y callbacks durables

### Prompt relevante

> "Implementa E3-022 y E3-023 de EnergyShark, respetando el enunciado E3 y AD7. Implementa el cliente Lambda para Wallet, usando X-Api-Key desde Secrets Manager, claves de idempotencia estables, manejo de 200/202, 429 con Retry-After, reintentos 5xx con backoff y límites de cuota. Implementa la persistencia durable y correlación del task token antes de invocar Wallet, contemplando la carrera en que el evento llega antes de la respuesta HTTP."

### Resumen de la respuesta y utilidad

Se revisó el `develop` actualizado y se comprobó que las dependencias de persistencia, migración, ASL, Lambdas e IAM habían quedado integradas entre ramas feature, pero todavía no estaban en `develop`. La nueva rama partió del head actual e incorporó esos commits una sola vez. La migración de task tokens se encadenó al head vigente del outbox unificado para evitar dos heads de Alembic.

El cliente Lambda obtiene la configuración Wallet mediante Secrets Manager y envía `X-Api-Key` sin registrarla. Cada escritura exige una `Idempotency-Key` recibida desde la operación y la conserva en timeouts, HTTP 429 y respuestas 5xx. Un HTTP 202 solo representa recepción; un HTTP 200 puede recuperar un estado ya conocido. Los errores 401, 404 y `IDEMPOTENCY_MISMATCH` se clasifican como permanentes. Los cupos de 120 escrituras y 300 lecturas por minuto se coordinan mediante la tabla PostgreSQL ya existente, incluida la propagación de `Retry-After` entre instancias.

Para los pasos con callback, la Lambda guarda y confirma el task token, la ejecución, el rol y las claves `idpk`/`swapId` antes de reservar cuota o abrir la conexión HTTP. Un replay de un token ya completado no vuelve a llamar a Wallet. La prueba de carrera usa una segunda sesión PostgreSQL para simular que el evento Wallet encuentra y completa el token mientras la respuesta HTTP todavía está pendiente.

El artefacto común de las Lambdas pasó a una imagen reproducible de Python 3.12 con `psycopg` fijado. Se comprobó localmente la construcción e importación de esa imagen. La validación incluyó **632 pruebas y 3 subtests de Master**, **160 pruebas y 12 subtests de Connector**, **26 pruebas de CDK/Lambda** y **8 pruebas focalizadas** de migración, cuota, persistencia y carrera. También pasaron `alembic upgrade head`, `alembic check`, la síntesis CDK y `git diff --check`, usando PostgreSQL temporal aislado.

Los cambios quedaron en la PR Backend **#109**, con los commits `253688a` y `24f928c`. No se desplegó AWS ni se activaron sagas; el feature flag seller permanece desactivado por defecto. Antes del despliegue falta resolver la conectividad privada entre Lambda y PostgreSQL, alinear el input seller actual con el ASL e implementar el relay de callbacks posterior.
