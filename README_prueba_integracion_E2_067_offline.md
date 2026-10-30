# Prueba de integración local E2-067 — sin servidor central

Esta prueba **no necesita acceso a EC2, RabbitMQ, Wallet, PostgreSQL, Docker ni JWT**. Comprueba la integración de los scripts QA/Data existentes (`qa.e2066` y `qa.e2067`) contra una API falsa que solo escucha en `127.0.0.1` y logs Wallet sintéticos. **No prueba que las compras o liquidaciones reales funcionen**, ni demuestra los resultados económicos de un experimento real.

## Uso

Colocar `probar_E2_067_integracion_offline.py` en la raíz de `E0-Arquisis-local`, con E2-066 y E2-067 previamente instalados:

```bash
python3 probar_E2_067_integracion_offline.py
```

Por defecto, los resultados de prueba se crean en una carpeta temporal y **se eliminan al terminar**. Los datos existentes del repositorio (`datos_e2066/` y `datos_e2067/`) no se modifican.

Para revisar CSV, JSON e informes sintéticos después de la prueba:

```bash
python3 probar_E2_067_integracion_offline.py --output-dir /tmp/e2067_mock_$(date +%s)
```

También puedes ejecutar el script desde otra carpeta usando `--repo /ruta/a/E0-Arquisis-local`.

## Qué comprueba

1. Genera un plan equilibrado de 5 spans y vincula cinco `cycleId` **inventados claramente etiquetados**.
2. Levanta un servidor HTTP de prueba en loopback, con las rutas reales de lectura: `/cycles/{id}/history`, `/market/cycle-metrics`, `/market/book`, `/market/cycle-metrics/compare`.
3. Usa los **programas de consola reales** de E2-067 para capturar fases `early` y `final` (reloj simulado, sin esperar ciclos de 20 minutos).
4. Valida respuestas HTTP 503 y 404, `cycleId` inesperado, protección contra sobrescritura, costo pendiente, divergencia de `span`, ausencia de captura final y ruta deshabilitada.
5. Importa telemetría Wallet sintética con `idpk` coincidente, `cycleId` explícito, observaciones no atribuibles, fuera de ventana, timeouts y HTTP 429. La atribución manual queda marcada como tal.
6. Consolida resultados de E2-067 y analiza métricas por span mediante E2-066, comprobando que las observaciones incompletas se excluyen.
7. Verifica que la salida no contiene el secreto ficticio inyectado en las respuestas y que todos los accesos al mock son **GET**, sin operaciones REST de escritura.

## Resultado esperado

Aparecerá la frase **`OK — integración local E2-066 / E2-065(mock) / E2-067`**, seguida de comprobaciones, incluido que **2 de 5 escenarios sintéticos son económicamente comparables**, que no se filtró información sensible y que no se envió ningún POST.

Los resultados conservados se encuentran en `datos_e2067_mock/resultados/` dentro de la carpeta indicada por `--output-dir`. Los informes están marcados **PRUEBA SIMULADA — NO SON RESULTADOS REALES DE RF06**.

## Límites

- La prueba simula la interfaz HTTP del backend y telemetría; **no usa una base de datos real ni ejecuta la lógica económica interna**.
- Confirma que el *pipeline* de registro de E2-067 funciona sin conectividad, pero **no permite cerrar el criterio de aceptación que exige ejecutar y registrar ciclos de mercado reales**.
- Cuando se restablezca la conexión a la central, será necesario ejecutar el plan experimental real, asociar `cycleId` oficiales y observar eventos/latencias verdaderos.
