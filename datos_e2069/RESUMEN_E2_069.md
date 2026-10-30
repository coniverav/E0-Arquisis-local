# E2-069 — Evidencia de integración local AD4

**Tipo:** SIMULACIÓN LOCAL, NO evidencia de Wallet Central real.
**Generado:** 2026-10-30T03:29:43.286040+00:00
**Escenarios aprobados y registrados:** 13

| Escenario | Oferta KWH | HOLD KWH | Reservados | Rechazados | available rechazos |
|---|---:|---:|---:|---:|---|
| 40ms_80_mas_80 | 100 | 80 | 1 | 1 | 20 |
| commit_remaining_idempotente | 100 | 40 | 2 | 1 | 40 |
| cupo_agotado_available_0 | 100 | 100 | 2 | 3 | 0, 0, 0 |
| dos_ofertas_independientes_A | 100 | 80 | 1 | 0 | — |
| dos_ofertas_independientes_B | 100 | 80 | 1 | 0 | — |
| idpk_duplicado_40ms | 100 | 70 | 1 | 0 | — |
| rafaga_40ms_ronda_1 | 100 | 60 | 1 | 7 | 40, 40, 40, 40, 40, 40, 40 |
| rafaga_40ms_ronda_2 | 100 | 60 | 1 | 7 | 40, 40, 40, 40, 40, 40, 40 |
| rafaga_40ms_ronda_3 | 100 | 60 | 1 | 7 | 40, 40, 40, 40, 40, 40, 40 |
| rafaga_40ms_ronda_4 | 100 | 60 | 1 | 7 | 40, 40, 40, 40, 40, 40, 40 |
| rollback_atomico | 100 | 0 | 0 | 0 | — |
| transacciones_superpuestas_40ms_120ms_lock | 100 | 80 | 1 | 1 | 20 |
| wallet_fake_sin_insufficient_asset | 100 | 80 | 1 | 1 | 20 |

## Interpretación

Se probó el ingreso FastAPI con sesiones PostgreSQL concurrentes, el `SELECT FOR UPDATE` por oferta, las reservas persistentes, la correlación con SwapRecord y las respuestas `bid-rejected`.
La prueba de la Wallet usa un **doble local**: permite comprobar que el backend no propone más KWH de los reservables, pero **no** demuestra ausencia de `INSUFFICIENT_ASSET` en la Wallet Central real.
No se estableció conexión a la central ni se publicaron mensajes AMQP reales.

## Pendiente de producción

Repetir el caso con dos compradores reales separados 40 ms, verificar los eventos oficiales `swap-*` y confirmar que no exista `swap-rejected` de origen propio con razón `INSUFFICIENT_ASSET` en KWH.
