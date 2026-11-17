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
