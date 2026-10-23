# Plan de milestones — E2

### Milestone 0: Preparación, spec y decisiones 17–18 Oct

* Auditoría de E1 y matriz de compatibilidad con el protocolo v3 (E2-001).
* Spec E2, milestones, paralelismo y responsables (E2-002–003).
* ADR de serialización por oferta, consistencia con wallet y estrategia configurable: AD4–AD6 (E2-005–007).
* Plan de post-mortems de AD1–AD3, AI logs y revisión de PRs (E2-004, E2-008).
* Referencia oficial de API v3, credenciales/colas y baseline de AWS (E2-009–011).
* Matriz inicial de pruebas y requisitos esenciales G01–G09 (E2-012).

### Milestone 1: Fundamentos del protocolo v3 18–20 Oct

* Consumo de `city.{CODE}` y `market.{CODE}` y separación de mensajes dirigidos/feed de mercado.
* Validación de `user_id`, ACK/NACK entre ciudades y prevención de respuestas infinitas.
* Persistencia de ofertas del feed y `market-closed` (E2-013–017).
* Modelos de wallet, swaps, ofertas, bids y estrategias; cliente REST con `X-Api-Key`.
* Escrituras con `Idempotency-Key` persistente y correlación de eventos `swap-*` (E2-018–022).
* Adaptación a ciclos de 20 minutos y tiempos informados por la central (E2-023).
* Schemas v3, pruebas iniciales de contratos, configuración por ciclo e instrumentación de wallet/consumidores (E2-024–027).

### Milestone 2: Mercado completo e integración de prueba 19–21 Oct

* Construcción del libro de ofertas y cálculo de transporte y precio efectivo (E2-035–036).
* Vendedor: publicar y actualizar ofertas, validar bids, serializar aceptaciones y responder rechazos.
* Propuesta de swaps y emisión de `bid-accepted` solo después de `swap-proposed` (E2-028–034).
* Comprador: seleccionar ofertas, enviar bids, procesar respuestas y verificar `swapToken` ES256/JWKS.
* Aceptación de swaps con idempotencia y manejo de resultados asíncronos (E2-037–041).
* Aplicación única de `swap-settled`, procesamiento de `trade-recorded`/`imbalance-settlement` y conciliación inicial con wallet (E2-042–044).
* Pruebas de reenvíos y duplicados, ensayo integrado con MET y despliegue/release de prueba antes del mercado puntuable (E2-045–048).

### Milestone 3: Mercado productivo y vistas E2 22–25 Oct

* Inicio del mercado de producción y evidencia de al menos un swap liquidado (G04; E2-049).
* Financiamiento v3, `creditLine`, `balanceAfter`, liquidación y reportes automáticos en la ventana de cierre (E2-050–052).
* Actualización de `remaining` y liberación de reservas del vendedor (E2-053).
* Compra autónoma según déficit, manejo de contraparte incumplidora y expiraciones (E2-054–055).
* Endpoints para historial E2, libro de ofertas, estrategias y swaps (E2-056–057).
* SPA: mercado en vivo, detalle de ciclos y estado de swaps (E2-058–060).
* Alertas y panel básico de errores de wallet y broker (E2-061).

### Milestone 4: Estrategias, resiliencia y experimento 26–29 Oct

* Estrategias editables y activación por ciclo sin redeploy: políticas de venta, aceptación y selección de compras (E2-062–064).
* Registro del `span` y métricas por ciclo; diseño y ejecución del experimento RF06 (E2-065–067).
* Recuperación ante HTTP 202 perdido, eventos de wallet desordenados y reconciliación automática (E2-068, E2-070).
* Pruebas de bids concurrentes con 40 ms de diferencia para AD4 (E2-069).
* Manejo de límites `429`/`Retry-After`, reintentos y feed de mercado con TTL/capacidad y resincronización (E2-071–072).
* Interfaz para políticas, próximo ciclo efectivo y comparación de resultados por `span` (E2-073–074).
* Auditoría extendida a operaciones wallet y mensajes entre ciudades (E2-075).

### Milestone 5: Contratos finales, documentación y QA 30 Oct–01 Nov

* Contratos v3 y pruebas CI contra schemas del curso; OpenAPI y contexto compartido actualizados (E2-076–077).
* Ajustes de frontend: auditoría, estados del mercado, errores y frescura del feed/rutas deshabilitadas (E2-078–080).
* Revisión de AD4–AD6 con evidencia y post-mortems de AD1–AD3 (E2-081–082).
* UML actualizado, guía de ejecución local e informe del experimento de `span` (E2-083–084).
* Revisión de AWS, autenticación, dominios, presupuesto y monitoreo de latencia wallet/broker (E2-085–086).
* Validación funcional completa en producción y preparación de las siete anomalías de demo: duplicados, identidad, contraparte, timeout wallet, corte broker, ruta deshabilitada y monitoreo (E2-087–094).
* Checklist G01–G09 y continuidad E0/E1; revisión de secretos, AI logs y flujo de PRs (E2-095–096).

### Milestone 6: Ensayo, release y entrega 02–03 Nov

* Ensayo de un ciclo completo de 20 minutos y de la demo de anomalías (E2-097).
* Congelamiento de release y verificación de despliegue productivo en AWS (E2-098).
* Cierre de spec, milestones, ADRs, UML, contratos, pruebas y AI logs (E2-099).
* Soak test y conciliación de balances con wallet a través de varios ciclos (E2-100).
* Guion y accesos preparados para el ayudante; todos conocen el codebase (E2-101).
* Entrega E2, conservación de evidencias y coordinación de la demo en vivo (E2-102–103).
