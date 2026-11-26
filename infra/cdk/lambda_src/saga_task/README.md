# Artefacto de tareas de saga

CDK empaqueta este directorio como una imagen común e inmutable para las
tareas. `seller-proposal`, `buyer-acceptance`, reconciliación y cancelación
usan Wallet con secretos de Secrets Manager y cuota PostgreSQL compartida.
Los callbacks registran el task token antes de cualquier llamada HTTP. La misma imagen despacha
las 13 tareas según `SAGA_TASK` y valida el rol antes del cierre.

El secret Wallet contiene `baseUrl` HTTPS y `apiKey`; el de base de datos usa
`DATABASE_URL` o los campos `host`, `port`, `username`, `password` y `dbname`.
Nunca se registran secretos, cuerpos completos, task tokens, JWT ni
`swapToken`. La imagen fija la versión de `psycopg-binary`; no se despliega en
esta rama.

## Reintentos y cuotas E3-049

`WalletConfig.max_attempts` y `maxAttempts` del secret admiten de 1 a 4
intentos HTTP por llamada, con 4 por defecto. El backoff exponencial parte
en 1 segundo, tiene un tope de 8 segundos y jitter uniforme entre la mitad
y el total de cada espera. Un `Retry-After` válido prevalece, incluido cero.
El contrato v3.0.2 define este header como segundos enteros. Si falta o es
inválido, se usa el backoff. Se reintentan 429, 500, 502, 503, 504 y errores
de transporte o respuesta incompleta. Todo 422 es terminal, y el campo
oficial `reason` identifica `IDEMPOTENCY_MISMATCH`.

Las ASL conservan cuatro reintentos de tarea, ahora con jitter completo y
tope de 8 segundos. Así, una tarea que efectúa una llamada Wallet tiene
hasta 20 intentos HTTP entre sus cinco invocaciones, acotados además por
los deadlines. La propuesta usa el `expiresAt` congelado y la aceptación
usa el mínimo del vencimiento del swap y cierre del ciclo. Ambas respetan
`timeoutAt` y el tiempo restante de Lambda con un segundo de margen.
La cancelación y consulta de reconciliación usan el presupuesto restante
que ya entrega E3-048.

Cada intento HTTP reserva y confirma un cupo antes de salir, incluso si
pierde la respuesta. `operational_rate_limits` mantiene una ventana móvil
de 60 segundos por ciudad y categoría, compartida con Master y protegida
por bloqueo de fila. Se usa el reloj PostgreSQL después de adquirir el
bloqueo y el límite menor publicado en el último `status-statement`, con
máximos de 120 escrituras y 300 lecturas por minuto. El bloqueo por
`Retry-After` queda persistido y una respuesta posterior con menor espera
no lo acorta. Se reutiliza la tabla existente de E2-071.

La llave, URL y cuerpo permanecen estables durante cada retry. HTTP 202
mantiene la espera del evento. El replay de propuesta HTTP 200 recupera
el vencimiento congelado para continuar la saga. La aceptación HTTP 200
terminal sigue esperando el hecho económico persistido. Un token repetido
conserva su vencimiento original. Los eventos tardíos y duplicados pasan
por la correlación y reconciliación de E3-047/E3-048.

Las pruebas específicas están en `infra/cdk/tests/test_e3049_wallet_retries.py`
y `master/tests/test_e3049_wallet_retries_db.py`. La segunda requiere una
base `energyshark_test_*` y aplica las migraciones existentes mediante el
conftest. Ambas usan Wallet simulado. La prueba de concurrencia comparte
el cupo entre 16 workers Lambda/Master y verifica 120 autorizaciones de
160 solicitudes.

Contrato consultado en el tag v3.0.2, commit
`62f6cf40768b6e42717d126e528dcd944e5bc0e3`.
[OpenAPI oficial Wallet](https://github.com/iic2173/energyshark-contracts/blob/v3.0.2/wallet.openapi.yaml).
