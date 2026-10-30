# E2-069 — Protocolo de prueba para AD4

## Qué garantiza la prueba local

Se valida el ingreso `/internal/messages` real, usando diferentes hilos y sesiones de PostgreSQL contra el mismo `MarketOffer`, con una separación **nominal** de 40 ms. La prueba **no presupone cuál mensaje obtiene primero el `SELECT FOR UPDATE`**: comprueba las invariantes al finalizar y la correlación de los mensajes de rechazo.

La reserva local se considera segura cuando:

1. `SUM(OfferReservation.quantity WHERE status='HOLD') <= MarketOffer.remaining_quantity`.
2. Cada bid reservado tiene exactamente un `SwapRecord` y una reserva `HOLD`, sin duplicación por `idpk`.
3. Los bids excedentes están `REJECTED` y generan una sola respuesta outbox `bid-rejected` con `reason=QUANTITY_UNAVAILABLE`, `code=409`, `target` del bid original y `available` entero correcto.
4. Antes de `swap-proposed`, no existe `bid-accepted`; un HTTP 202 no significa liquidación.
5. El cambio a `COMMITTED` afecta `remaining` una sola vez y un `ROLLBACK` revierte toda la decisión.

## Casos cubiertos

| Caso | Oferta | Solicitudes | Resultado esperado |
|---|---:|---|---|
| Dos compradores, 40 ms | 100 | 80 + 80 | Una reserva de 80, otro rechazo con `available=20` |
| Ráfaga repetida, 40 ms | 100 | 8 solicitudes de 60 | Una reserva, siete rechazos `available=40` |
| Disponibilidad agotada | 100 | 60 + 40, después 3 x 10 | Dos reservas, tres rechazos `available=0` |
| Reentrega del mismo `idpk` | 100 | 70 dos veces | Un solo bid, HOLD y SwapRecord |
| Despacho con Wallet fake | 100 | 80 + 80 | Un único POST idempotente; cero fallas simuladas `INSUFFICIENT_ASSET` |
| COMMIT repetido | 100 | 60; COMMIT x2; 41; 40 | `remaining=40`, rechazo 41, HOLD 40 |
| Rollback | 100 | Reserva 50 revertida | Sin HOLD/SwapRecord; bid sigue PENDING |
| Locks superpuestos | 100 | 80 + 80 con lock 120 ms y separación 40 ms | Una reserva y rechazo `available=20` |
| Ofertas independientes | 100 cada una | 80 + 80 a ofertas distintas | Ambas se reservan, sin contaminación cruzada |

## Ejecución

```bash
./probar_E2_069.sh
MODO=relacionados ./probar_E2_069.sh
E2069_ROUNDS=10 ./probar_E2_069.sh
```

No se realiza HTTP a Wallet Central ni AMQP al broker oficial. Se utiliza un doble explícito de Wallet y evidencia marcada `SIMULACION_LOCAL_SIN_CENTRAL`.

## Límites

- 40 ms es el retardo nominal entre inicios de solicitudes, no un SLA de ejecución ni garantía de orden real de adquisición de locks; su demora se registra en los JSON.
- Las pruebas corren en una sola computadora, pero con conexiones concurrentes independientes al mismo PostgreSQL. Para probar comportamiento distribuido real se necesita desplegar ambas réplicas.
- La falta de `INSUFFICIENT_ASSET` frente a Wallet Central **no puede demostrarse** mediante una Wallet simulada; debe contrastarse contra su balance y eventos oficiales cuando exista acceso.
- No se escribe en la base real ni se publican ofertas al mercado externo.
