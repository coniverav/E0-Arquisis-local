## Prompt

Cómo está funcionando el healthcheck? (RNF7) cómo se comprueba que funciona correctamente?

## Respuesta

Sí. En tu proyecto el RNF7 está implementado de tres formas, una para cada tipo de container. El enunciado pide que todos los containers tengan un HEALTHCHECK que verifique que su servicio está operativo, pudiendo usar curl o comprobación de archivos.

master y master2

En master/Dockerfile tienes:
```
HEALTHCHECK --interval=20s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/health || exit 1
```
Cada 20 segundos Docker ejecuta:
```
curl http://127.0.0.1:8000/health
```
Y /health en FastAPI hace además:
```
connection.exec_driver_sql("SELECT 1")
```
Por lo tanto no comprueba solamente:

“FastAPI está encendido”.

También comprueba:

“FastAPI está encendido y puede comunicarse con PostgreSQL”.

Si todo funciona devuelve:
```
{
  "status": "ok",
  "instance": "master"
}
```
Si PostgreSQL falla, devuelve HTTP 503, curl falla y Docker empieza a considerar al container no saludable.
---
db

PostgreSQL tiene:
```
HEALTHCHECK --interval=10s --timeout=5s --start-period=10s --retries=5 \
  CMD pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" || exit 1
```
pg_isready es una utilidad de PostgreSQL que pregunta:

“¿PostgreSQL está listo para aceptar conexiones?”

Si responde correctamente:
```
exit code = 0
```
Docker marca:
```
healthy
```
Si falla repetidamente:
```
unhealthy
```
---
connector

Aquí no tiene sentido crear una API HTTP solamente para hacer healthcheck. De hecho, el enunciado dice que no es necesario.

Por eso utilizamos un archivo heartbeat.

connector.py ejecuta permanentemente:
```
async def heartbeat():
    while True:
        HEARTBEAT.touch()
        await asyncio.sleep(10)
```
Es decir, cada 10 segundos actualiza:
```
/tmp/energyshark_connector_heartbeat
```
Y healthcheck.py comprueba la antigüedad del archivo:
```
age = time.time() - HEARTBEAT.stat().st_mtime
```
Si tiene menos de 60 segundos:
```
sys.exit(0)
```
→ healthy.

Si tiene más de 60 segundos o no existe:
```
sys.exit(1)
```
→ Docker eventualmente lo marca unhealthy.

Esto tiene además una ventaja importante: RabbitMQ puede estar temporalmente caído y connector continuar healthy, porque el requisito dice que connector debe seguir vivo intentando reconectarse.

Cómo comprobar que está funcionando

Tu prueba más simple ya la hiciste:
```
docker compose ps
```
y obtuviste:
```
connector   Up ... (healthy)
db          Up ... (healthy)
master      Up ... (healthy)
master2     Up ... (healthy)
```
Eso demuestra que Docker está ejecutando correctamente los healthchecks.

Pero puedes verlo en más detalle.

Para master:
```
docker inspect master --format "{{json .State.Health}}"
```
Para PostgreSQL:
```
docker inspect db --format "{{json .State.Health}}"
```
Para connector:
```
docker inspect connector --format "{{json .State.Health}}"
```
Ahí deberías ver algo del estilo:
```
"Status":"healthy"
```
junto con los últimos intentos y sus ExitCode.

La mejor prueba: hacer que falle a propósito

Para demostrar que realmente funciona, puedes detener PostgreSQL:
```
docker stop db
```
Después espera unos segundos y revisa:
```
docker compose ps
```
master terminará perdiendo acceso a PostgreSQL y su /health debería empezar a fallar.

Luego vuelve a levantar:
```
docker compose up -d db
```
y, una vez recuperada la BD, master debería volver eventualmente a:
```
healthy
```
También puedes probar directamente:
```
curl http://localhost:8001/health
```
Normalmente devuelve:
```
{"status":"ok","instance":"master"}
```
---
Un detalle importante del connector

Nuestro healthcheck actualmente comprueba:

“el proceso connector sigue vivo”.

No comprueba:

“RabbitMQ está disponible en este preciso momento”.

Y eso es intencional.

Si RabbitMQ cae temporalmente:
```
RabbitMQ ❌
connector ✅ vivo, reintentando
```
el connector debe seguir siendo considerado operativo, porque RNF1 exige precisamente que no termine permanentemente ante una caída del broker. Si atáramos el HEALTHCHECK directamente a RabbitMQ, podríamos marcar como unhealthy un connector que en realidad está haciendo exactamente lo que se pide: seguir vivo y reconectarse.

Por eso, para RNF7, la implementación que tienes es coherente con la arquitectura de la tarea.