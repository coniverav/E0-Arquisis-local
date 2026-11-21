# Plan de milestones — E3

### Milestone 0: Continuidad, spec y decisiones 16–17 Nov

* Auditar continuidad E1/E2 en producción: cloud, ambas colas, protocolo, ciclos, reportes, HTTPS, Auth0, Wallet, New Relic y AWS Budgets (E3-001).
* Versionar spec E3, arquitectura objetivo, decisiones pendientes y criterios de aceptación medibles (E3-002).
* Preparar ADR7 de sagas y ADR8 de outbox antes de cualquier commit funcional (E3-003–004).
* Redactar post-mortems de AD4 y AD6 con evidencia real de E2, sin atribuciones causales no demostradas (E3-005–006).
* Alinear Jira, milestones, dependencias, AI logs y política de revisión de PRs (E3-007–008).
* Dibujar UML objetivo, inventariar AWS/costos, ejecutar Prowler inicial y fijar suite base/matriz de gates (E3-009–013).

### Milestone 1: Cimientos de sagas, outbox e IaC 18–20 Nov

* Elegir Terraform o CDK y definir módulos, estado, ambientes, secretos y estrategia de despliegue (E3-014).
* Diseñar y migrar almacenamiento durable de sagas, ejecución, `idpk`, `swapId`, task tokens y deadlines sin borrar historia E2 (E3-015–016).
* Diseñar máquinas Standard de vendedor y comprador; declararlas junto con Lambdas, logging e IAM mínimo (E3-017–021).
* Implementar cliente Lambda Wallet idempotente y persistir token antes de invocar HTTP; resolver callbacks tardíos o duplicados (E3-022–024).
* Diseñar outbox único para RabbitMQ y `StartExecution`, migraciones aditivas, relays con confirms y coordinación `SKIP LOCKED` (E3-025–031).
* Definir contrato de eventos SSE, endpoint autenticado y replay por `Last-Event-ID` (E3-032–034).
* Probar ASL, callbacks, atomicidad del outbox y definir las métricas base de E3 (E3-035–037).

### Milestone 2: Flujos completos e integración 20–23 Nov

* Integrar saga vendedora: reserva AD4, propuesta Wallet, espera, `bid-accepted`, terminales, compensación y actualización de oferta (E3-038–043).
* Integrar saga compradora: inicio único, verificación ES256/plazos, aceptación, espera y reconciliación (E3-044–049).
* Migrar reportes de cierre y ofertas propias al outbox sin cambiar identidades ni ventanas (E3-050–051).
* Construir frontend SSE sin polling, resolver autenticación/ruta y mostrar mercado, Wallet, sagas y alertas (E3-052–054).
* Desplegar Step Functions, Lambda, IAM, relays y migraciones desde IaC con backup y rollback (E3-055–057).
* Lograr un swap productivo `SETTLED` por saga, demostrar continuidad de ciclo/reporte y ejecutar integración seller/buyer y SSE (E3-058–062).

### Milestone 3: Resiliencia, alertas y seguridad 24–25 Nov

* Configurar y probar cinco alertas: Wallet lenta, consumidores atrasados, delta ledger–Wallet, saga atascada y outbox atrasado (E3-063–069).
* Escribir runbook por alerta con diagnóstico, primera acción segura y escalamiento (E3-070).
* Seleccionar y reparar tres hallazgos Prowler MEDIUM+; ejecutar y conservar el reporte posterior (E3-071–075).
* Declarar el stack previo en IaC, revisar permisos/secretos y probar reconstrucción sin destruir producción (E3-076–078).
* Inyectar caída post-commit y corte del broker; demostrar recuperación del relay (E3-079).
* Medir AD7 y AD8: sagas/reservas al cierre, objetivos por ciclo y backlog recuperado en hasta 60 segundos (E3-080–082).
* Ejecutar regresión E1/E2 y contratos v3.0.2 en CI (E3-083).

### Milestone 4: Evidencia, documentación y entrega 26–27 Nov

* Completar ADR7/ADR8 con costos, métricas reales, duplicaciones y resultados de recuperación, preservando la decisión previa (E3-084–085).
* Finalizar post-mortems AD4/AD6, UML E3 y runbook con enlaces operables (E3-086–088).
* Cerrar checklist G01–G05 con evidencia productiva y faltantes explícitos (E3-089).
* Ensayar incidente guiado, recorrido UML y ronda individual; preparar preguntas técnicas (E3-090–093).
* Confirmar budgets para Lambda/Step Functions, secretos, AI logs, revisiones y protección de ramas (E3-094–095).
* Congelar release, validar rollback, conservar evidencias y coordinar la demo con el ayudante (E3-096–098).
