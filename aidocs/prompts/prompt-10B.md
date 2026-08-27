## Prompt

por qué me sale este error?

energyshark_connector  | [connector] error procesando mensaje: HTTPConnectionPool(host='master', port=8000): Max retries exceeded with url: /events (Caused by NewConnectionError("HTTPConnection(host='master', port=8000): Failed to establish a new connection: [Errno 111] Connection refused"))

## Respuesta

Ese error significa que el connector intentó avisarle a master de un evento, pero nadie contestó en master:8000 — el contenedor de master no estaba escuchando en ese momento (no es problema de RabbitMQ ni del connector, es que el otro lado no estaba disponible).

Antes de tocar código, revisemos qué está pasando con master:

1. Revisa si master sigue vivo
Ejecuta: docker compose ps. Fíjate en la columna de estado del contenedor energyshark_master: debe decir 'running' o 'healthy'. Si dice 'restarting' o 'exited', ahí está la causa.
2. Busca un crash en los logs de master
Ejecuta: docker compose logs master --tail=80. Busca si el proceso se cayó con algún traceback justo antes de que empezaran los errores de conexión rechazada, o si simplemente nunca llegó a decir 'Uvicorn running on http://0.0.0.0:8000'.
3. Prueba master directamente
Entra a http://localhost:8000/health desde tu navegador mientras el error sigue ocurriendo. Si tampoco responde ahí, confirma que master está realmente caído (no es solo un problema de red entre contenedores).
4. Revisa si un cambio reciente rompió el reload
Si vienes de usar 'docker compose up --watch' y modificaste algún archivo de master/app mientras probabas, es posible que ese cambio haya introducido un error de sintaxis o import que hace que uvicorn --reload se caiga al reiniciar. Revisa si tu último cambio guardado en esos archivos quedó completo y sin errores.