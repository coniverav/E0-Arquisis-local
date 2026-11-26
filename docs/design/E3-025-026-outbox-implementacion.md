# E3-025 / E3-026 — Contrato y migración del outbox unificado

## ADR y alcance

Implementación **aditiva** de `docs/adr/ADR-008-outbox-confiable.md`.
Usa **la tabla E2 `outbound_messages`**; no crea una outbox paralela.

Estas dos tarjetas incorporan el contrato, la persistencia transaccional,
las columnas/índices, unicidad del inicio de saga y claims concurrentes con lease.
**No** despliegan ni habilitan un relay nuevo, ni migran aún todos los productores
críticos al helper; esos pasos pertenecen a las siguientes tarjetas de AD8.
Mantener la ruta E2 del connector activa hasta un cutover probado.

## Modelo y campos

- `delivery_kind`: `BROKER` (default para filas E2) o `STEP_FUNCTIONS`.
- Para Broker: `msg_id`, `idpk`, `message_type`, `payload` (envelope v3 completo),
  `exchange_name`, `routing_key`, `cycle_id`, `swap_id` opcional.
- Para Step Functions: `idpk` de entrada, `execution_name = idpk`, ARN de máquina,
  `execution_input` JSON serializado **una sola vez**, `message_type=SAGA_START`,
  `msg_id` de intención local única y `cycle_id`. El JSON de inicio debe
  incluir `idpk`, `role` y `cycleId` coincidentes con la fila.
- Comunes: `status`, `attempt_count`, `available_at`, `expires_at`, `created_at`,
  `dispatch_required`, `last_error`, `lease_owner`, `claim_token`, `lease_until`,
  `completed_at`.
- Las filas Broker existentes mantienen `msg_id UNIQUE`; `idpk` **NO** es único
  globalmente, porque varios reintentos v3 comparten la misma llave.
- Índice UNIQUE parcial `(state_machine_arn, execution_name)` para
  `STEP_FUNCTIONS`, evitando dos intenciones de la misma ejecución.
- Un trigger PostgreSQL impide alterar los IDs, payload, input y destino
  tras insertar una intención. El estado de despacho sí es mutable.

## Contratos programáticos

- `outbox_store.enqueue_broker(session, envelope, exchange_name, routing_key, ...)`.
- `outbox_store.enqueue_saga_start(session, idpk, role, state_machine_arn,
  execution_input, cycle_id, ...)`.
- Ninguno llama `commit()`, RabbitMQ ni AWS. El servicio de negocio hace
  `session.add(...)`, llama al helper y ejecuta **un solo commit**.
- El `execution_input` de Step Functions se guarda como texto JSON exacto.
  En un reintento no se le añade un timestamp, identificador ni campo nuevo.
- El `outbox_contract` copia profundamente los envelopes de entrada antes
  de persistirlos para evitar cambios por referencias mutables del llamador.

## Claims y fencing

```
PENDING / FAILED --claim--> CLAIMED --confirm Broker--> PUBLISHED
                                  \---confirm AWS-----> STARTED
                                  \---fallo------------> FAILED
CLAIMED con lease vencido --claim nuevo--> CLAIMED
```

El relay invoca `claim_batch` en una transacción y hace **commit antes de la red**.
La consulta usa `SELECT ... FOR UPDATE SKIP LOCKED`, permite leases vencidos,
respeta `available_at`/`expires_at` y conserva el orden oferta -> revisiones
para una ciudad/ciclo. `finish_claim` bloquea la fila y comprueba
`claim_token` antes de confirmarla; una confirmación tardía pierde el derecho.
`attempt_count` cuenta los claims, **no** las confirmaciones duplicadas.

**Limitación fundamental:** si el broker acepta el mensaje y se pierde la
respuesta antes de marcar la fila, el relay publica otra vez tras recuperar
el lease. Es entrega **al menos una vez**, no exactamente una publicación;
RabbitMQ/consumidores y Step Functions deben deduplicar por `idpk` / nombre.

La integración futura debe conservar estos invariantes:

1. El viejo `/internal/audit/outbound/dispatch` no debe operar en paralelo
   sobre las mismas filas que el claim nuevo. Implementar cutover atómico
   por destino o sustituir su API de despacho; NO arrancar ambos a la vez.
2. `finish_claim(..., outcome='PUBLISHED')` requiere publisher confirms reales.
3. `finish_claim(..., outcome='STARTED')` requiere `StartExecution` confirmado,
   o corroborar `ExecutionAlreadyExists` para mismo ARN, nombre e input.
4. Reintentos de `FAILED` se programan con `retry_at` y conservan los IDs.
5. El relay debe respetar cuotas y ventanas del protocolo; no saltarse
   `reserve-quota`, `validUntil` ni el límite de 120 escrituras Wallet/minuto.
6. La publicación `negotiation-report` requiere preservar toda la lógica
   de vigencia, rectificaciones y confirmación existente en E2.

## Operación segura

- La migración agrega columnas/índices y un trigger a la tabla existente;
  no borra historia. Puede requerir locks breves en PostgreSQL durante DDL.
- **No aplicar a la DB productiva con esta tarjeta.** Ejecutar primero
  tests en DB temporal y acordar ventana/backups con el equipo de despliegue.
- Downgrade rechaza si hay intenciones `STEP_FUNCTIONS`, para no perder datos.
- La serie de tests de E2 debe permanecer verde. Si el trigger detecta un
  productor viejo que modifica la intención tras insertarla, investigar
  el productor: no quitar el trigger sin evaluar implicaciones de AD8.

## Criterios de aceptación

- [ ] Una sola tabla; filas E2 intactas (`BROKER`).
- [ ] Un `commit` hace durables negocio e intención; rollback revierte ambos.
- [ ] Envelope v3, `msgId`, `idpk`, ARN/nombre/input inmutables.
- [ ] Nombre de ejecución igual al idpk original, índice único parcial.
- [ ] Dos relays concurrentes reclaman filas distintas; otro puede reclamar
      una fila después de caducar el lease.
- [ ] El claim anterior no puede confirmar después de la reclamación nueva.
- [ ] Una actualización de oferta no adelanta a la oferta inicial.
- [ ] Pruebas del nuevo módulo y regresión E2 en PostgreSQL de prueba.

## Reportes de cierre E3-050

El scheduler mantiene un solo commit para el estado del ciclo, el reporte y
su intención en `outbound_messages`. La generación reutiliza la última
versión si los saldos coinciden. Las correcciones generan otro `idpk` y
`msgId`. Un retry de transporte conserva el envelope completo. La instrucción
`REPORT_TOO_EARLY` conserva saldos e `idpk` y crea el intento de negocio E2
con nuevo `msgId`, disponible en `opensAt`. `CYCLE_EXPIRED` termina el despacho.

El relay verifica la ventana de la Central y vence las intenciones incluso
con el scheduler detenido. Las versiones de reporte se reclaman en orden por
ciclo. La generación espera un claim vigente antes de corregir el reporte.
Una confirmación tardía pierde autoridad cuando la intención fue sustituida
o rechazada. Antes de publicar, Connector verifica `expiresAt` y `leaseUntil`
y acota su timeout a ambos. Solo `Basic.Ack` permite finalizar la fila.

Las cuotas existentes continúan aplicándose por categoría. El contrato de
`negotiation-report` no le asigna la cuota de ofertas, lecturas o escrituras
Wallet. Un reporte puede salir aunque esos cupos estén agotados.

### Cutover y reversión

`E3_CYCLE_REPORTS_OUTBOX_ENABLED=false` mantiene el despacho E2 y excluye
reportes de los claims del relay. Con `true`, Master los entrega por claims y
los excluye del dispatcher E2. También rechaza autorizaciones y resultados
legados de esos reportes. La configuración local transmite el flag a ambas
réplicas de Master y conserva `false` como valor predeterminado.

Para cambiar el modo, detener y drenar los publicadores anteriores, configurar
el mismo valor en todas las réplicas de Master y arrancar el Connector con
`E3_BROKER_OUTBOX_RELAY_ENABLED=true`. Ese flag selecciona el relay en lugar del
bucle E2 del Connector. El scheduler puede seguir persistiendo intenciones
durante el corte. Elegir un momento con margen suficiente antes de `validUntil`.
Una réplica con configuración antigua o un publicador que ya recibió permiso
podría aún enviar, por lo que el drenaje forma parte del cambio de modo.

Para revertir, detener y drenar el relay, poner ambos flags en `false` y
arrancar el publicador E2. Los claims vigentes permanecen excluidos hasta que
venza su lease. El dispatcher recupera luego esas mismas filas con sus IDs y
payload originales. Las configuraciones y secretos productivos requieren la
operación de despliegue autorizada por el equipo.

La recuperación es al menos una vez y puede repetir una publicación cuando
se pierde la confirmación local. La Central deduplica la operación por `idpk`.
Si la Central ya cerró la ventana, la recuperación no puede garantizar evitar
la multa. Tampoco un publisher confirm acredita aceptación del reporte por la
Central. La meta de recuperación AD8 presupone disponibilidad del broker y
margen de ventana suficiente.

Pruebas específicas en `master/tests/test_e3050_cycle_reports_outbox.py` y
regresión de Connector en `connector/tests/test_e3029_e3030_relays.py`, con
PostgreSQL `energyshark_test_*` y broker simulado.

[Contrato oficial v3.0.2](https://github.com/iic2173/energyshark-contracts/blob/v3.0.2/schemas/amqp/negotiation-report.schema.json).
