# E3-053 · Ruta HTTPS para el panel en vivo

La ruta propuesta es `GET https://<host-sse>/events/stream`. El navegador abre una conexión TLS al Nginx del host EC2. La plantilla `nginx/sse.conf.template` reutiliza el upstream `energyshark_backend` de `nginx/energyshark.conf`. Las dos réplicas de Master comparten el cursor de PostgreSQL, así que la reconexión con `Last-Event-ID` puede llegar a cualquiera de ellas.

**Dependencias.** En backend `develop` en `c2e8e14` están E3-033 (PR #130) y E3-055 (PR #136). E3-052 está integrada en frontend `develop` en `3000eb7`. El panel usa fetch incremental, bearer y checkpoint. [PR frontend #29](https://github.com/VicenteIgnacioSotoGonzalez/Arquisis-G10-Frontend/pull/29)

## Elección de ruta

El inventario E3-010 y `ExistingInfrastructureStack._api` identifican una **HTTP API v2** con integración HTTP proxy, autorizador JWT de Auth0 y CORS. El dominio documentado es `api.energyshark-g10.tech`. CloudFront con origen S3 y OAC sirve la SPA en `app.energyshark-g10.tech`. El repo declara Nginx como balanceador de los puertos locales 8001/8002 de EC2. No hay un ALB declarado en este trayecto. El archivo Nginx existente menciona otro dominio y certificado; el operador debe verificar el virtual host activo y el DNS de staging antes de instalar la plantilla. Estos datos proceden del repo y requieren contraste con la cuenta.

AWS admite `responseTransferMode=STREAM` en REST API para integraciones `HTTP_PROXY` y `AWS_PROXY`. El máximo documentado es 15 minutos, con timeout de inactividad de 5 minutos en Regional/Private o 30 segundos en Edge. El heartbeat de Master es de 15 segundos. La HTTP API v2 actual carece de ese modo. El host independiente evita migrar el API o introducir el caché y las reescrituras de la distribución de la SPA en el trayecto del feed. [Streaming en API Gateway](https://docs.aws.amazon.com/apigateway/latest/developerguide/response-transfer-mode.html)

## Autorización y operación

Master valida la firma RS256 con las JWKS de Auth0 y exige los valores configurados de `iss`, `aud` y `exp`. El router verifica además `sub`, el permiso `read:events` (o `SSE_REQUIRED_SCOPE`) y las revocaciones persistidas. Ambas réplicas deben recibir la misma configuración. Una petición sin bearer o con token inválido o expirado recibe 401. Una identidad sin el permiso o revocada recibe 403 antes de abrir el feed. Si el token expira o se revoca durante el stream, Master cierra la conexión. El cliente obtiene un token vigente y reanuda desde su último evento completo.

Nginx admite GET y OPTIONS en la ruta exacta, rechaza parámetros de consulta y devuelve 404 en otras rutas del virtual host. El preflight llega al middleware CORS de Master. Configurar en ambas réplicas `CORS_ALLOWED_ORIGINS` con los orígenes exactos de las SPA autorizadas, incluido esquema y puerto si corresponde. Evitar comodines. El cliente envía `Authorization` y, al reanudar, `Last-Event-ID`. El permiso de lectura lo decide el JWT incluso si el cliente omite Origin.

La respuesta tiene `Cache-Control: no-cache, no-store, no-transform`. Nginx desactiva caché, compresión y buffering y espera hasta 60 segundos entre lecturas del upstream. Los comentarios heartbeat mantienen viva una conexión sin eventos y no avanzan el cursor. El log dedicado registra identificador de petición, método, status, duración, bytes y status del upstream al cerrar la conexión. Observar 401/403 antes de apertura, 499, 502/504 y 503 del proyector/replay. Para un 200 ya iniciado, medir la llegada de dos tramas separadas y del heartbeat desde el cliente; el status final de Nginx no acredita la progresividad. Mantener tokens, parámetros, cuerpos y datos de eventos fuera de las evidencias.

## Preparación y rollback en staging

1. Identificar el EC2 de staging, el upstream, las rutas de certificados y el DNS administrado. Verificar que el certificado cubre el host SSE, que TLS llega directamente a Nginx y que los puertos de Master sólo escuchan en loopback. Registrar DNS y TTL previos.
2. Renderizar la plantilla en un archivo temporal. Los valores de las tres variables son el FQDN y las rutas absolutas del certificado y su clave ya aprovisionados. La sustitución explícita conserva las variables propias de Nginx.

   ```bash
   : "${SSE_HOST:?}" "${SSE_CERT:?}" "${SSE_KEY:?}"
   export SSE_HOST SSE_CERT SSE_KEY
   candidate=$(mktemp)
   envsubst '${SSE_HOST} ${SSE_CERT} ${SSE_KEY}' < nginx/sse.conf.template > "$candidate"
   ```

3. Tras aprobación, incorporar el candidato junto a la configuración que declara `energyshark_backend`, validar con `nginx -t` y recargar Nginx de forma gradual. Antes de publicar DNS, usar `curl --resolve "$SSE_HOST:443:$STAGING_IP"` con el certificado válido. Publicar el registro A del host SSE sólo después de verificar autenticación y streaming. Un registro AAAA requiere también un listener y conectividad IPv6 verificados.
4. **Integración pendiente en la SPA.** En `3000eb7` el servicio acepta `openSseFeed({apiUrl})`, pero el panel omite esa opción y hereda `VITE_API_URL`. Para activar el host independiente, el frontend debe pasar la base HTTPS exclusiva del feed y mantener la base vigente para los demás endpoints. Verificar en el navegador del origen desplegado el preflight, dos eventos en la misma petición y la reconexión. [Cliente SSE del frontend](https://github.com/VicenteIgnacioSotoGonzalez/Arquisis-G10-Frontend/blob/3000eb7/src/services/sse.js)
5. Para revertir, retirar primero la selección del nuevo host en el panel, restaurar el registro DNS anterior (o retirar el nuevo registro si no existía) y esperar su TTL. Drenar las conexiones existentes considerando la expiración de los JWT. Retirar sólo el virtual host añadido y validar con `nginx -t` antes de recargar. La API HTTP y el dominio de la SPA conservan sus rutas.

## Comprobación reproducible

Con PostgreSQL de pruebas desechable en UTF-8, `DATABASE_URL` apuntando a una base **exclusiva** llamada `energyshark_test_*`, y Nginx, OpenSSL y curl instalados, ejecutar desde `master/`. La fixture aplica las migraciones existentes y limpia las tablas de esa base entre pruebas. La prueba HTTPS crea su propio certificado efímero y valida su cadena localmente.

```bash
export CORS_ALLOWED_ORIGINS=https://frontend.test
timeout 210 .venv/bin/python -m pytest -q tests/test_e3053_sse_https.py tests/test_e3032_e3033_sse.py tests/test_auth.py
```

Para comparar IaC localmente, usar el mismo CDK CLI, Node y entorno Python para ambas síntesis. Desde la raíz del backend, con las dependencias de `infra/cdk` instaladas y `cdk` en el PATH:

```bash
set -eu
export PATH="$PWD/infra/cdk/.venv/bin:$PATH"
work=$(mktemp -d)
mkdir "$work/base"
git archive c2e8e14:infra/cdk | tar -x -C "$work/base"
(cd "$work/base" && timeout 120 cdk synth --all --no-lookups -o "$work/base.out" >/dev/null)
(cd infra/cdk && timeout 120 cdk synth --all --no-lookups -o "$work/branch.out" >/dev/null)
stack=EnergySharkDev-ExistingInfrastructure
timeout 120 cdk diff --app "$work/branch.out" "$stack" --template "$work/base.out/$stack.template.json"
for stack in ExistingInfrastructure Monitoring Sagas; do
  cmp "$work/base.out/EnergySharkDev-$stack.template.json" "$work/branch.out/EnergySharkDev-$stack.template.json"
done
```

En una cuenta de staging autorizada, confirmar protocolo, integración, rutas y autorizador con comandos de lectura. Obtener el ID de la API del inventario de ese ambiente.

```bash
: "${AWS_PROFILE:?}" "${AWS_REGION:?}" "${STAGING_API_ID:?}"
export AWS_PROFILE AWS_REGION
aws sts get-caller-identity --query Account
aws apigatewayv2 get-api --api-id "$STAGING_API_ID" --query '{type:ProtocolType,cors:CorsConfiguration}'
aws apigatewayv2 get-integrations --api-id "$STAGING_API_ID" --query 'Items[].{type:IntegrationType,connection:ConnectionType}'
aws apigatewayv2 get-routes --api-id "$STAGING_API_ID" --query 'Items[].{route:RouteKey,auth:AuthorizationType}'
aws apigatewayv2 get-authorizers --api-id "$STAGING_API_ID" --query 'Items[].{type:AuthorizerType,jwt:JwtConfiguration}'
```

En staging, verificar desde el origen real del panel y con un operador de pruebas autorizado. La variable `TOKEN` contiene un access token de corta vida. El último comando pasa el bearer por entrada estándar de curl. El timeout de curl es un límite intencionado para un stream que puede seguir abierto.

```bash
url="https://${SSE_HOST:?}/events/stream"
curl -isS --max-time 10 "$url"                            # 401
curl -isS --max-time 10 -X OPTIONS "$url" \
  -H "Origin: ${SPA_ORIGIN:?}" -H 'Access-Control-Request-Method: GET' \
  -H 'Access-Control-Request-Headers: Authorization,Last-Event-ID'
printf 'Authorization: Bearer %s\n' "${TOKEN:?}" | \
  curl -iN --max-time 45 -H @- -H "Origin: $SPA_ORIGIN" "$url"
```

Repetir con un token válido sin scope para comprobar 403. Con el token autorizado, generar dos hechos de prueba mediante el flujo normal en momentos distintos y registrar sólo tiempos e IDs. Repetir con el último ID recibido en `Last-Event-ID`. Confirmar que la conexión termina al expirar el token, que no se duplica el checkpoint y que un origen ajeno carece de autorización CORS de lectura.

## Evidencia local de la tarjeta

- SSE, replay y autenticación: `27 passed in 58.87s`. Por la misma conexión HTTPS, curl recibió el ID 1 antes de insertar el segundo hecho, después recibió el ID 2 y finalmente un heartbeat. Al reanudar con `Last-Event-ID: 1`, recibió únicamente el ID 2 y la conexión terminó con el JWT de corta duración.
- Con el proxy activo se verificaron 401 sin bearer, 403 sin permiso, preflight CORS para el origen permitido, rechazo del preflight ajeno, 405 para POST, 400 con query y 404 fuera de la ruta.
- La síntesis CDK de la rama y de la base `c2e8e14` produjo tres plantillas idénticas. El diff local del stack de infraestructura devolvió `Number of stacks with differences: 0`. El resultado compara código versionado. El estado de AWS, el TLS público y el navegador del origen desplegado quedan pendientes de validación en staging.
