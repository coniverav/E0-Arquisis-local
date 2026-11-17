# ADR-007: Sagas orquestadas para swaps

## Contexto y escenario de calidad

El protocolo v3, los mensajes del mercado y la Wallet se mantienen en la E3. Lo que cambia es la coordinación interna de un swap: en E2 quedó repartida entre el consumidor, servicios del backend y el scheduler. Ese diseño conservaba reservas e identificadores, pero una operación podía quedar esperando eventos externos o reintentos en varios componentes sin un historial único de su avance.

El escenario que orienta esta decisión tiene seis partes:

- **Fuente:** una contraparte y la Wallet.
- **Estímulo:** un `swap-proposed` llega tarde, la contraparte no acepta o no llega el evento de liquidación.
- **Ambiente:** mercado abierto, con la latencia normal de Wallet y dos réplicas de backend.
- **Artefacto:** coordinación de swaps de comprador y vendedor, más el consumidor que recibe eventos.
- **Respuesta esperada:** cada swap termina como liquidado, rechazado o compensado; no deja una reserva local retenida y una reentrega no inicia otra operación.
- **Medida:** no quedan ejecuciones abiertas 60 segundos después de `closesAt`, ni reservas locales retenidas al cierre. Además, se medirán por ciclo el tiempo desde el inicio hasta el estado terminal y el número de compensaciones por timeout.

## Alternativas evaluadas

1. **Mantener la coreografía de E2.** Los consumidores, el scheduler y los servicios actuales continúan intercambiando estados y ejecutando los reintentos.
   - Conserva la infraestructura actual y evita incorporar un servicio nuevo.
   - Sin embargo, deja la coordinación temporal repartida y no entrega un historial único de una operación ni una espera durable de eventos externos.

2. **Una sola máquina de estados para ambos roles.** Una ejecución recibe el rol y bifurca entre vendedor y comprador.
   - Centraliza pasos comunes y reduce el número inicial de definiciones.
   - Mezcla dos flujos cuyas reservas, credenciales, compensaciones y condiciones de término son diferentes. El historial incluiría ramas irrelevantes y cambios de un rol podrían afectar al otro.

3. **Dos máquinas AWS Step Functions Standard, una por rol.** Cada máquina usa tareas Lambda, espera callbacks con task token y aplica reintentos y compensaciones propios.
   - Separa responsabilidades, facilita el diagnóstico y permite terminar o compensar cada rol de manera explícita.
   - Obliga a mantener dos definiciones y a persistir la correlación entre operación local, ejecución y task token.

4. **Dos máquinas por rol, trasladando también el ledger a Lambda.** Las funciones ejecutarían las escrituras económicas y los consumidores quedarían sólo como transporte.
   - Haría visibles más pasos dentro de las máquinas de estado.
   - Duplicaría o reemplazaría garantías de PostgreSQL ya implementadas, aumentaría las condiciones de carrera entre Lambda y el consumidor y exigiría nuevas decisiones de conectividad y permisos a la base.

## Decisión

Se adoptarán **dos máquinas de estados AWS Step Functions Standard**, una para swaps donde KLD vende y otra para swaps donde compra. Standard es necesario para conservar historial visible y esperar callbacks con task token; el nombre de ejecución será el `idpk` que inicia la operación: el del `bid` para el vendedor y el de `bid-accepted` para el comprador.

Antes de llamar a Wallet, la saga deberá guardar durablemente el identificador de la operación, el ARN de ejecución y, cuando entre a una espera, el task token y su clave de correlación. Una reentrega con el mismo `idpk` consultará ese registro y reutilizará la misma ejecución; no generará otra reserva, otra llave de idempotencia ni otro swap. `StartExecution` de Standard es idempotente mientras la ejecución está activa cuando recibe el mismo nombre e input; una ejecución ya cerrada requiere tratar `ExecutionAlreadyExists` como parte del estado local, no como una orden de iniciar otra operación.

La saga de vendedor cubrirá: reservar localmente, proponer el swap con la llave persistida, esperar `swap-proposed` o `swap-rejected`, publicar `bid-accepted`, esperar el término, reconciliar por `swapId` si falta un evento, cancelar cuando corresponda y compensar liberando la reserva y actualizando la oferta. La de comprador verificará `swapToken`, plazo y activos, aceptará con su llave persistida, esperará el hecho terminal y reconciliará ante una espera vencida. HTTP `202` sólo confirma recepción de la solicitud: no confirma liquidación.

## Fronteras de responsabilidad

La orquestación temporal, los reintentos HTTP, las esperas, los timeouts y las compensaciones viven en Step Functions y Lambda. El consumidor de EC2 mantiene la suscripción a RabbitMQ, validación de identidad, deduplicación de mensajes y aplicación idempotente de los eventos de Wallet. El ledger y sus efectos económicos permanecen en PostgreSQL y son actualizados por el camino actual del consumidor; la estrategia comercial de AD6 sigue decidiendo qué comprar o vender fuera de la saga.

El consumidor reanudará una ejecución mediante `SendTaskSuccess` o `SendTaskFailure` sólo después de persistir el hecho económico y localizar el token correspondiente. Los eventos que no tengan la clave ideal se correlacionarán por `swapId`, sin adivinar una relación. Un token vencido o una ejecución ya terminal se registrará como tal y no reabrirá una operación.

## Objetivos y costo a validar

Los siguientes son objetivos de aceptación para medir durante E3, no resultados ya obtenidos:

| Objetivo | Meta inicial | Evidencia esperada |
| --- | --- | --- |
| Ejecuciones abiertas después de `closesAt + 60 s` | 0 por ciclo | Historial de Step Functions y consulta de sagas locales |
| Reservas `HOLD` asociadas a saga terminal o vencida | 0 por ciclo | Consulta de reservas y resultado de compensación |
| Inicio duplicado por reentrega del mismo `idpk` | 0 ejecuciones adicionales | Registro local + ARN/nombre de ejecución |

El costo se estimará y alertará usando transiciones reales. Como referencia, la tarifa publicada para un workflow Standard en US East (N. Virginia) es USD 0,000025 por transición, después de 4.000 transiciones mensuales gratuitas. Una proyección de **4.320 swaps/mes** (dos por cada uno de 72 ciclos diarios), con **10 transiciones** en la ruta normal, da 43.200 transiciones/mes; descontado el nivel gratuito, serían aproximadamente USD 0,98/mes para Step Functions con esa tarifa de referencia. No incluye reintentos, Lambda, logs, transferencia ni el precio específico de la región desplegada. Antes de producción se reemplazará la proyección por transiciones y duración reales de CloudWatch/Pricing Calculator y se configurará una alerta de presupuesto para Step Functions y Lambda.

## Consecuencias y tradeoffs

La decisión privilegia recuperación, trazabilidad y compensación explícita sobre una solución más simple. Introduce recursos, permisos, costos por transición y una tabla de correlación adicional. Las Lambdas pueden ejecutarse más de una vez, por lo que cada tarea debe conservar la misma identidad económica e `Idempotency-Key`. También requiere límites explícitos en cada espera: los callbacks con task token no tienen un timeout útil por defecto para este caso.

No se modifica la autoridad de la Wallet ni el ledger local. El resultado económico seguirá viniendo de los eventos y la conciliación; Step Functions coordina el proceso, no transforma un `202` en una liquidación.

## Referencias

- [Enunciado E3, secciones 2 y 5](../../../Enunciado%20E3%20Arquisis.pdf).
- [AWS Step Functions: StartExecution](https://docs.aws.amazon.com/step-functions/latest/apireference/API_StartExecution.html).
- [AWS Step Functions: callback con task token](https://docs.aws.amazon.com/step-functions/latest/dg/connect-to-resource.html).
- [AWS Step Functions Pricing](https://aws.amazon.com/step-functions/pricing/).
