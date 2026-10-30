# E2-067 — Protocolo de registro de ciclos reales (RF06)

**Fuentes:** E2-066 plan de spans; E2-065 métricas por ciclo; E2-027 HTTP Wallet; E2-048 despliegue estable. **No se han ejecutado ciclos reales al instalar este paquete.**

## Evidencia mínima por ciclo

- Slot del plan, `cycleId`, versión de estrategia AD6 y `spanSeconds` realmente usados.
- Captura temprana después de abrir mercado y captura final después de `closesAt`; ambas con timestamp y entorno/despliegue.
- Cronología de mensajes de negocio y hechos económicos a partir del historial persistido (no payloads AMQP crudos).
- Métricas E2-065: rechazos, energía comprada, cobertura, costo real, baseline, estado `FINAL` o pendiente.
- Telemetría E2-027 de latencia REST Wallet por endpoint, con correlación comprobada o etiqueta de atribución manual.
- Variaciones externas: precio efectivo, rutas habilitadas/penalizadas, liquidez aparente, ventanas oficiales y notas observacionales.

## Pasos por slot

1. Antes de la apertura, usar `qa.e2066.configurar_span --slot N ... --apply`. Esa configuración es **PENDING** y se activa en el siguiente ciclo. Verificar que se haya aplicado a dicho ciclo.
2. Registrar el `cycleId` real con `qa.e2066.registrar_ciclo --slot N --cycle-id ...`.
3. Durante el mercado tomar `qa.e2067.capturar --slot N --phase early ...`.
4. Una vez cerrado el mercado y recibido el settlement, tomar `qa.e2067.capturar --slot N --phase final ...`. Si falta settlement, documentar la ausencia y repetir captura con `--refresh` al recibirlo.
5. Exportar logs `wallet_http` del despliegue y usar `qa.e2067.latencias`. No atribuir llamadas por horario sin advertirlo explícitamente; mercados de distintos ciclos pueden solaparse.
6. Generar consolidado con `qa.e2067.consolidar`; E2-066 puede analizar cobertura/costos tras volver a recolectar E2-065.

## Advertencias

- Las capturas no detienen ni aceleran la central y no envían bids. La ejecución real la realiza el backend desplegado; estos scripts documentan su comportamiento.
- Una captura de `/market/book` no prueba qué ofertas estaban disponibles en un instante anterior. Usar dos capturas y registrar sus tiempos.
- El `202` de Wallet no implica liquidación: solo `swap-settled` confirma el intercambio.
- Una latencia sin `cycleId` o `idpk` verificable no debe atribuirse a un ciclo de forma automática.
- Una captura final anterior a `closesAt` se marca como no apta para comparación económica.
- Mantener el despliegue y las políticas constantes salvo el `span` estudiado; registrar cambios de mercado. No afirmar causalidad con un experimento observacional.
- No subir a GitHub `datos_e2066`, `datos_e2067`, logs sin sanitizar, JWT, archivos `.env` ni tokens.

## Criterios de aceptación

Scripts ejecutables y pruebas locales; evidencia real guardada en disco por `cycleId`, eventos, métricas, latencia Wallet y factores externos; informe por ciclo y valor de span con advertencias explícitas sobre datos faltantes.
