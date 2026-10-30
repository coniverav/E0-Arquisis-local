# E2-067 — Ejecutar y registrar ciclos del experimento de span (QA / Data)

## 1. Qué hace

Añade **scripts de observación y registro de ciclos reales**, para completar la evidencia que se diseñó en E2-066. No simula resultados reales ni opera Wallet ni cambia la estrategia directamente. Se apoya en E2-066 para configurar spans/registrar slots y en E2-065 para leer métricas de RF06.

Cada ciclo se registra por `cycleId`, con **eventos**, **capturas de precios/liq. y rutas**, **latencias de Wallet E2-027**, política efectiva y **estado de costo real/baseline**. Los scripts generan CSVs e informe Markdown.

## 2. Instalar

Prerequisitos: E2-065 instalado y corregido (`ON DELETE CASCADE`), E2-066 instalado, Docker/backend del proyecto accesible, Python 3.10+. Desde la raíz del repositorio:

```bash
python3 aplicar_e2_067.py
chmod +x probar_E2_067.sh
./probar_E2_067.sh
```

El instalador es idempotente; no sobrescribe diferencias sin `--force` y hace backup si se usa esa opción. **No requiere migración Alembic.** Las pruebas no necesitan Docker ni acceso al mercado y **no ejecutan ciclos reales**.

## 3. Ejecutar realmente UN ciclo y registrar evidencia

**A.** Revisar el plan y configurar el primer span *antes* de que abra un nuevo ciclo:

```bash
python3 -m qa.e2066.planificar --blocks 5  # solo si todavía NO tienes plan
python3 -m qa.e2066.configurar_span --slot 1 --base-url http://localhost:8001
# Previo: export RF06_JWT="..."  (token válido; no poner en archivos Git)
python3 -m qa.e2066.configurar_span --slot 1 --base-url http://localhost:8001 --apply
```

El `POST` crea una versión AD6 **PENDING**. No la considera aplicada al ciclo actual. Esperar a que la central abra el siguiente ciclo. No ejecutar `planificar` si ya tienes `cycleId` anotados: recrearía el CSV y perderías las asociaciones.

**B.** Identificar el `cycleId` real desde la respuesta `/cycles` o el historial del backend (no inventarlo):

```bash
curl -s http://localhost:8001/cycles
python3 -m qa.e2066.registrar_ciclo --slot 1 --cycle-id ID_REAL --regime NORMAL \
  --notes "Primer mercado: captura a 30 s de opensAt"
```

Si el puerto `localhost:8001` es un túnel hacia producción, usa `--environment production` en vez de `local`; la etiqueta debe reflejar el entorno REAL consultado.

**C.** Durante el mercado (idealmente justo después de opensAt, a un instante repetible) capturar eventos y situación de ofertas:

```bash
python3 -m qa.e2067.capturar --slot 1 --phase early \
  --base-url http://localhost:8001 --environment local \
  --deploy-sha SHA_DEL_COMMIT_REAL
```

**D.** Tras el cierre del mercado y el settlement de Wallet:

```bash
python3 -m qa.e2067.capturar --slot 1 --phase final \
  --base-url http://localhost:8001 --environment local \
  --deploy-sha SHA_DEL_COMMIT_REAL
```

Si `status` es distinto a `FINAL`, está pendiente algún dato. Para volver a capturar **solo esa fase** cuando llegue un evento tardío, repetir `capturar --phase final ... --refresh`. La herramienta valida `cycleId` y `span` real; si difieren, registra la discrepancia.

**E.** Exportar `wallet_http` de los logs (no subir el archivo a Git). Por ejemplo, desde contenedores locales:

```bash
docker compose logs --no-color master master2 cycle-scheduler > datos_e2067/wallet_http_raw.log
python3 -m qa.e2067.latencias --slot 1 --file datos_e2067/wallet_http_raw.log
```

Las solicitudes Wallet que carezcan de `cycleId` y `idpk` correlacionable se excluyen. Si el archivo fue filtrado y contiene **solo llamadas del ciclo indicado**, existe `--assume-isolated`; esa atribución es **manual/no verificada** (atención al solapamiento de ciclos). `latencias` guarda estadísticas, no los logs crudos ni los idpk, en `datos_e2067/wallet/<cycleId>.json`.

**F.** Consolidar y verificar los datos:

```bash
python3 -m qa.e2067.consolidar
python3 -m qa.e2066.recolectar --base-url http://localhost:8001
python3 -m qa.e2066.analizar
```

Repetir A-F para los siguientes slots reales, cada uno con span diferente según el plan. El frontend es **opcional**: el flujo principal se ejecuta en la **terminal** y el backend debe estar conectado al mercado real de la central.

## 4. Archivos generados

- `datos_e2067/evidencias/001_<cycleId>/early.json`: lectura al inicio.
- `datos_e2067/evidencias/001_<cycleId>/final.json`: lectura tras el cierre.
- `datos_e2067/wallet/<cycleId>.json`: estadísticas de latencia E2-027.
- `datos_e2067/resultados/registro_ciclos.csv`: una fila por slot con span real y costo.
- `datos_e2067/resultados/eventos_por_ciclo.csv`: hechos con identificadores y timestamps.
- `datos_e2067/resultados/resumen_por_span.csv`: agregados solo de ciclos con evidencia apta.
- `datos_e2067/resultados/informe_E2_067.md`: faltantes y limitaciones.

No se inventan registros si todavía no hay ciclos. El paquete pone `.gitignore` dentro de `datos_e2067` para evitar subir resultados y logs por accidente.

## 5. Cuándo está listo

- **Pruebas locales:** verifican parsing, atribución, generación de informes, flags e integridad.
- **Ensayo real E2-067:** solo está ejecutado cuando existen ciclos reales con `cycleId` del backend, dos capturas y métricas Wallet/mercado; los 25 slots completos se registran si se pretende ejecutar el plan E2-066 completo.
- Si solo hay simulaciones E2-066 o `0/25` ciclos, **NO** se puede afirmar que E2-067 haya sido ejecutado en producción.

## 6. Observabilidad y privacidad

Solo se guardan campos autorizados y estadísticas de latencia, nunca JWT, X-Api-Key, headers completos, cuerpos HTTP/AMQP, `swapToken` o `swapToken` en las capturas. El endpoint `/market/book?includeInactive=true` contiene un snapshot del feed y **no** constituye un historial de ofertas completo. Para analizar mercados cambiantes se guardan capturas de instantes distintos con timestamps. Un `wallet_http` sin id de ciclo/operación no se atribuye automáticamente por proximidad temporal.
