# Objetivo

La E2 busca evolucionar el nodo energético construido en E0 y E1 para participar de forma **autónoma en un mercado entre ciudades** bajo el protocolo v3. Cada ciudad deberá publicar y mantener ofertas de venta, solicitar energía a otras ciudades, responder bids y completar intercambios mediante la **wallet REST de la central**, que pasa a ser la fuente de verdad de créditos (`CR`) y energía (`KWH`).

El sistema deberá conservar el funcionamiento básico de los ciclos, la idempotencia y auditoría de mensajes, adaptarse a ciclos de 20 minutos, mantener una interfaz de administración y un historial consultable, permitir estrategias configurables sin redeploy y continuar operando en AWS con monitoreo de la API, las dos colas y la wallet.

# Alcance

* Migración de la mensajería de E1 al **protocolo v3**, conservando las reglas compatibles y retirando los flujos obsoletos.
* Consumo de las dos colas RabbitMQ: `city.{CODE}` para mensajes dirigidos y eventos, y `market.{CODE}` para el feed del mercado.
* Publicación y recepción de `offer`, `offer-updated`, `offer-withdrawn`, `bid`, `bid-accepted`, `bid-rejected` y `market-closed`.
* Cliente REST de wallet para proponer, aceptar, cancelar y consultar swaps; consultar balances, movimientos y leaderboard; y verificar los tokens firmados de swaps.
* Operación automática como **vendedor y comprador**, con control de cantidades ofrecidas, reservas, políticas de aceptación y selección de ofertas.
* Aplicación persistente e idempotente de eventos de wallet y conciliación de saldos locales frente a la wallet.
* Gestión de ciclos de 20 minutos y envío autónomo de `negotiation-report` dentro de su período de cierre.
* Procesamiento de `status-statement`, `transfer` de financiamiento, `distance-table`, `trade-recorded` e `imbalance-settlement`.
* Historial por ciclo, libro de ofertas vigente, administración de estrategias, estado de swaps, auditoría de anomalías y experimento de `span` en frontend.
* Mantención de contratos JSON, OpenAPI, pruebas automatizadas, infraestructura AWS, autenticación y monitoreo.
* Documentación de ADR4–ADR6, revisión de ADR1–ADR3, artefactos de proceso, experimento y evidencias de demo.

# Fuera de alcance

* No se implementará una wallet propia ni se considerará el ledger local como autoridad económica. Los activos y sus retenciones son administrados por la wallet de la central.
* No se conservará el flujo comercial E1 basado en `negotiation-proposal`, `give`, `take`, `transfer` de pago o `demand-statement`: estos tipos fueron retirados en v3. El `transfer` de **financiamiento** sigue vigente.
* No se coordinarán directamente transferencias de energía fuera de los swaps de la wallet.
* No se asumirá que el `remaining` anunciado por otras ciudades corresponde a energía garantizada: el libro de ofertas es información potencialmente desactualizada.
* No se implementarán funcionalidades previstas para entregas posteriores; por ejemplo, el retorno de `demand-statement` mencionado para E3.
* No se controlará ni garantizará la estrategia o disponibilidad de otras ciudades, ni un lugar particular en el leaderboard.

# Supuestos y dependencias

* Se reutilizarán los componentes existentes de E1 cuando sean compatibles: connector RabbitMQ, API FastAPI, PostgreSQL, SPA React, autenticación, ECR/EC2, S3/CloudFront y observabilidad.
* Las definiciones exactas de mensajes, errores, cuerpos HTTP y schemas se contrastarán con **los contratos publicados por el curso**. Este documento define alcance y aceptación, no reemplaza dichos contratos.
* La central es la autoridad temporal: los instantes se obtienen de `status-statement.data.market` y `validUntil`; no se calculan a partir del número de `cycleId` ni exclusivamente del reloj local.
* La wallet puede responder con latencia, `202`, errores de capacidad y cortes de conexión después de procesar la escritura. Se debe disponer de persistencia para conocer qué operaciones se intentaron y recuperarlas.
* Se mantendrán secretos (`X-Api-Key`, credenciales de broker, JWT/configuración sensible) fuera de los repositorios.

# Requisitos funcionales

* **RF01 — Historial de ciclos:** para cada `cycleId`, mostrar `status-statement`, financiamiento, oferta y actualizaciones, bids enviados/recibidos y resultados, swaps y estado final, costo de transporte, `imbalance-settlement`, `negotiation-report` y última operación aplicada.
* **RF02 — Mercado en vivo:** presentar el libro de ofertas recibido por `market.{CODE}`, con vendedor, precio, `remaining`, distancia/transporte propio, precio efectivo y señalización de rutas deshabilitadas según la última `distance-table`.
* **RF03 — Mercado autónomo:** como vendedor, publicar/actualizar/retirar la oferta, serializar la aceptación de bids por oferta, proponer swaps y traducir rechazos de wallet a `bid-rejected`; como comprador, seleccionar ofertas, enviar bids, verificar `swapToken` y aceptar swaps. En ambos roles, manejar timeouts, reintentos idempotentes y aplicar eventos de wallet al historial local.
* **RF04 — Administración del mercado:** permitir configurar desde el frontend el precio y cantidad de la oferta, política de aceptación de bids, `span` de compra y política de selección de ofertas, sin redeploy. Mostrar swaps pendientes y resultados finales.
* **RF05 — Auditoría:** registrar y consultar mensajes duplicados, descartados y respondidos con NACK, incluidos los provenientes de otras ciudades, especialmente `IDENTITY_MISMATCH`. Ningún duplicado deberá reaplicar efectos ni proponer otro swap.
* **RF06 — Experimento de span:** registrar por ciclo el `span` efectivamente utilizado, déficit cubierto vía mercado, costo efectivo frente al baseline y bids rechazados. Mostrar resultados por ciclo y agregados por valor de `span`, distinguiendo observaciones de conclusiones.

# Reglas principales del protocolo

Se mantiene el envelope y la distinción de E1 entre `msgId` (mensaje particular) e `idpk` (operación lógica), junto con la validación de identidad AMQP `user_id`, ACK/NACK, errores y registro de anomalías. Los mensajes dirigidos de otras ciudades siguen esas reglas; **los mensajes del feed `market.{CODE}` no se responden con ACK de protocolo**.

En el mercado existe **una oferta por ciudad y ciclo**, con `offerId` propio. El vendedor la anuncia mediante `offer`, la actualiza con `offer-updated` (`remaining`, `pricePerEnergy`) y la retira mediante `offer-withdrawn`. El comprador envía a `city.{vendedor}` un `bid` con `offerId` y `quantity`: el bid **no lleva precio** y el vendedor lo acepta o rechaza completo. Un bid repetido con el mismo `idpk` conserva su identidad y respuesta sin repetir sus efectos.

Al aceptar un bid, el vendedor propone un swap con `give = KWH(quantity)`, `take = CR(round2(quantity × pricePerEnergy))` y `memo = offerId`. Solo después de `swap-proposed` envía `bid-accepted` con `swapId`, token firmado y vencimiento. El comprador verifica el JWT **ES256** usando JWKS (`sub`, `proposer`, activos/cantidades, `memo` y `exp`) antes de llamar a la wallet para aceptar.

Las escrituras REST de wallet requieren `X-Api-Key` y `Idempotency-Key` (UUID); el endpoint JWKS es público. Un `202 Accepted` confirma **recepción, no ejecución**. Los eventos de wallet son la confirmación de los resultados. Ante errores temporales, pérdida de respuesta o eventos tardíos se recuperará el estado o se reintentará usando **la misma llave**, sin crear una operación nueva. Los swaps expiran en un máximo de 60 segundos y sus retenciones deben resolverse correctamente.

Se deben respetar los límites operativos publicados por la central (incluidos `Retry-After` en HTTP 429): 60 operaciones de mercado, 120 escrituras de wallet, 300 lecturas de wallet y 10 `request/ask` por ciudad y minuto, según el enunciado. El feed de mercado tiene TTL de un ciclo y capacidad máxima de 5.000 mensajes.

# Ciclos, wallet y ledger

Cada ciclo dura **20 minutos**: 5 de consumo sin mercado, 10 de mercado y 5 de cierre. La ventana negociadora de E1 continúa en los últimos 15 minutos; el mercado del ciclo siguiente se solapa con el consumo del actual. Los instantes `opensAt`, `lastOfferAt`, `lastBidAt`, `closesAt`, `reportOpensAt` y `validUntil` son los definidos por la central. Las actualizaciones de ofertas deben respetar `lastOfferAt`, la aceptación de bids `lastBidAt`, y la negociación de swaps termina en `closesAt`.

La energía vendible acreditada corresponde a `sellable = max(0, generationCapacity - consumption)`. La wallet mantiene balances `available` y `held` en `CR` y `KWH`, además de `creditLine` para créditos. **Los CR se arrastran entre ciclos y pueden ser negativos dentro de las reglas de crédito; los KWH no se arrastran**. Al cierre se cancelan swaps pendientes, se liquida el desbalance y se extingue la energía restante.

La posición energética se expresa como `coverage = KWH en cuenta − max(0, consumption − generationCapacity)`. Si es positiva, se recibe una recompra a `buybackPrice = round2(1.05 × generationCost)`; si es negativa, se paga el déficit a `imbalancePrice = round2(3 × máximo generationCost del ciclo)`. La liquidación llega mediante `imbalance-settlement`; el transporte se cobra al **comprador** y se informa mediante `trade-recorded`, utilizando la tabla de distancias vigente para dicho comprador (o tarifa punitiva en ruta deshabilitada).

El **ledger local deja de ser fuente de verdad**. Los eventos de wallet se almacenarán y aplicarán idempotentemente, distinguiendo liquidación de swap y transporte. Se contrastarán los saldos con `GET /balance` y, cuando corresponda, `GET /statement`, preservando el historial y explicando discrepancias sin sobreescrituras silenciosas.

El `negotiation-report` conserva el formato y reglas de E1: envío durante el cierre, correcciones con un `idpk` nuevo, reintentos con el mismo `idpk`, manejo de `REPORT_TOO_EARLY`/`CYCLE_EXPIRED` y multa de 50.000 CR cuando corresponda. El ciclo debe reportarse autónomamente si la central efectivamente lo abrió/contactó, siguiendo las condiciones de E1.

# Mercado autónomo, estrategias y recuperación

El nodo deberá operar como vendedor y comprador sin intervención humana en cada ciclo. Como vendedor, anunciará una oferta coherente con sus activos disponibles, atenderá bids según la política configurada, registrará compromisos pendientes y evitará doble aceptación de energía. Como comprador, evaluará las ofertas del feed, sus precios efectivos considerando transporte y restricciones de ruta, enviará bids y dará seguimiento al resultado de los swaps.

Las decisiones arquitectónicas se documentarán **antes de sus implementaciones**:

* **AD4 — Serialización:** definir coordinación entre réplicas para que bids simultáneos sobre una misma oferta no comprometan dos veces los mismos KWH; incluir pruebas con bids separados por 40 ms y disponibilidad insuficiente.
* **AD5 — Consistencia eventual:** definir aplicación de eventos de wallet una sola vez, correlación de operaciones, recuperación de HTTP 202 perdido, tratamiento de eventos desordenados y reconciliación frente a `GET /balance`.
* **AD6 — Estrategia configurable:** separar políticas comerciales de ejecución, persistir las configuraciones y dejar trazabilidad de la estrategia usada por ciclo. Los cambios hechos a mitad de ciclo deberán entrar en vigencia **desde el siguiente ciclo**, sin reiniciar ni redesplegar.

La implementación exacta de bloqueos, tablas de reservas, estados y activación de configuración se fijará en los respectivos ADR y tareas de diseño; no se presupone como funcionalidad ya existente.

# Requisitos esenciales (gates de corrección)

* **G01:** sistema completo operativo en EC2, no solo en local.
* **G02:** consumo de ambas colas y ACK/NACK correcto también para mensajes de otras ciudades.
* **G03:** ciclo autónomo que procesa `status-statement`, eventos de wallet e `imbalance-settlement` y envía `negotiation-report` dentro de la ventana.
* **G04:** evidencia de al menos **un swap liquidado** durante el mercado de producción, como vendedor o comprador.
* **G05:** API JSON y SPA estática React/Vue en repositorios separados.
* **G06:** frontend y backend sobre HTTPS.
* **G07:** autenticación/autorización operativa con configuración JWK estándar.
* **G08:** monitoreo SaaS instalado y operativo.
* **G09:** alertas de presupuesto AWS configuradas.

Estos gates son requisitos de entrada: según el enunciado, si alguno falta, la nota máxima es 4,0.

# Requisitos de infraestructura y arquitectura

* **RNF01:** API publicada detrás de AWS API Gateway con subdominio propio y CORS correcto.
* **RNF02:** API Gateway valida autenticación antes de enrutar las solicitudes protegidas.
* **RNF03:** SPA desplegada en S3 y distribuida por CloudFront.
* **RNF04:** imágenes del backend publicadas en ECR y desplegadas sobre EC2 usando Docker Compose de producción.
* **RNF05:** monitoreo APM y de infraestructura, incluyendo consumo de la cola de mercado y llamadas/latencia de wallet.
* **AD4–AD6:** justificar cada mecanismo mediante ADR con escenario de calidad de seis partes (fuente, estímulo, ambiente, artefacto, respuesta y medida).

# Documentación y proceso

* **RDOC01:** ADR4–ADR6 con alternativas y tradeoffs; cada commit de ADR deberá anteceder los commits de su implementación. Incluir post-mortems reales de AD1–AD3 frente a los cambios de E2.
* **RDOC02:** mantener esta spec de alcance, supuestos y criterios antes de implementar; plan de milestones y AI logs reales por integrante (o declaración expresa de no uso). No simular fechas, aprobaciones ni commits anteriores.
* **RDOC03:** UML de componentes actualizado que muestre ambas colas, cliente wallet y ubicación de estrategia; guía de ejecución local y reproducción del monitoreo.
* **RDOC04:** contratos v3 en el repositorio organizacional, OpenAPI de API propia y contract tests en CI para mensajes emitidos según schemas oficiales; contexto común referenciado desde backend y frontend.
* **RDOC05:** informe del experimento de `span`, con valores probados, ciclos, métricas y limitaciones de inferencia por condiciones cambiantes del mercado.
* **Proceso con IA:** conservar logs concordantes con el uso real; si se usa programación agéntica, respetar revisión de PR por al menos dos compañeros y protección de la rama principal.

# Criterios de aceptación

Se propone validar el alcance mediante evidencias observables; **estos criterios describen lo que deberá demostrarse y no certifican funcionalidades ya implementadas**.

1. **Dos colas y compatibilidad (G02/RF05):** el nodo recibe mensajes de `city.{CODE}` y `market.{CODE}`; responde ACK/NACK solo donde corresponde, detecta `IDENTITY_MISMATCH` y deja registro consultable de duplicados, descartes y NACK sin duplicar efectos.
2. **Mercado visible (RF02):** al recibir `offer` y luego `offer-updated` o `offer-withdrawn`, la vista refleja el estado correcto; recalcula precio efectivo con transporte y destaca rutas `enabled: false` cuando cambia la tabla.
3. **Venta autónoma (RF03/AD4):** publica y actualiza oferta; recibe bids simultáneos cuya suma supera el `remaining`; no compromete energía dos veces; los rechazados quedan explicados y no se crean dos swaps sobre la misma cantidad.
4. **Compra autónoma (RF03):** selecciona oferta, envía bid, valida firma y claims de `swapToken` antes de aceptar; un token inválido o vencido no produce aceptación.
5. **Wallet e idempotencia (RF03/AD5):** procesa un `swap-settled` duplicado, otro fuera de orden respecto de `trade-recorded` y una escritura con respuesta HTTP perdida, manteniendo un solo efecto contable por hecho económico y reintentos con igual `Idempotency-Key`.
6. **Saldo oficial (AD5):** el balance del dashboard concuerda con `GET /balance` al cierre o expone una diferencia explicable y trazable desde los registros propios, sin ajustes silenciosos.
7. **Ciclo y reporte (G03/RF01):** en ciclos completos se registran eventos, fondos, swaps, transporte, liquidación y reporte; `negotiation-report` se envía en su ventana y se visualiza la última operación.
8. **Estrategia sin redeploy (RF04/AD6):** modificar `span`, política o precio durante un ciclo no reinicia contenedores; el siguiente ciclo utiliza la configuración nueva y ambos ciclos conservan qué versión se ejecutó.
9. **Experimento (RF06):** la vista presenta por ciclo y agrupados por `span` los indicadores exigidos, y el informe documenta condiciones, observaciones y límites de interpretación.
10. **Operación en AWS (G01/G05–G09/RNF01–RNF05):** servicios productivos disponibles por HTTPS, gateway y autorización verificables, frontend S3/CloudFront, backend ECR/EC2, monitoreo del mercado/wallet y budget alerts.
11. **Participación real (G04):** se puede mostrar al menos un `swap-settled` del mercado de producción, posterior al inicio del período de puntaje del 22 de octubre de 2026.
12. **Demo de anomalías:** el sistema soporta y permite explicar las anomalías inducidas del enunciado (duplicados, identidad ajena, contraparte incumplidora, timeout posterior a escritura, corte de broker, ruta deshabilitada y monitoreo), incluso si el ayudante elige dos escenarios no conocidos de antemano.
13. **Contratos y documentos (RDOC01–RDOC05):** repositorios y `/docs` contienen los ADR con secuencia de commits verificable, post-mortems, spec, milestones, AI logs, UML, instrucciones locales, contratos y pruebas CI, además del informe de `span`.