## Prompt

"explícame en palabras simples pero de manera detallada qué hace cada uno de
los archivos (todos) que me adjuntaste en el zip final. Al mismo tiempo,
explícame eso y cómo es que debería haber sido el flujo real si yo lo hubiera
hecho sola, como si fuera una narración de todo. Es decir, dame un texto que
al leerlo explique a cabalidad y en palabras simples los archivos, cómo es que
se debieron haber hecho a mano (sin IA) y por qué de cada decisión no trivial."

## Respuesta

Con esto ya tengo claro el estado final de todo. Te lo explico como un relato, archivo por archivo, en el orden en que naturalmente los habrías ido creando si lo hicieras tú sola sin ayuda.

La idea general antes de escribir código

El enunciado pide dos programas que se hablan entre sí:

- Algo que esté todo el rato escuchando RabbitMQ (el connector).
- Algo que guarde esos datos y los muestre por internet como una API (el master).

Se piden separados a propósito: si el broker se cae, tu API debe seguir funcionando y mostrando lo que ya tenías guardado. Si estuvieran mezclados en un solo programa, una caída de RabbitMQ podría tumbar también la API. Por eso, antes de escribir una línea, la primera decisión de diseño es: dos carpetas, dos programas, dos contenedores.

---

* master/app/database.py — conectar a la base de datos

Esto sería lo primero que programarías, porque todo lo demás depende de tener dónde guardar los datos.

Aquí se hacen tres cosas:

Se arma la URL de conexión a Postgres (usuario, contraseña, host, nombre de la base). En vez de escribirla fija en el código, se lee desde una variable de entorno (DATABASE_URL), porque en local usas una base y en AWS probablemente uses otra — no quieres tener que editar el código para cambiar de ambiente.
Se crea el engine, que es el objeto que sabe cómo hablar con Postgres.
Se define get_db(), una función chica que abre una conexión, la entrega, y al final siempre la cierra (el try/finally asegura que se cierre pase lo que pase, incluso si algo falla en el medio). Esto evita que se te acumulen conexiones abiertas y la base termine rechazando nuevas peticiones.

Si lo hicieras a mano, este archivo nace de leer la documentación de SQLAlchemy (la librería que traduce Python a SQL) y copiar el patrón estándar de "engine + sesión". No hay mucho que inventar aquí, es más bien plomería.

* master/app/models.py — cómo se ve una fila en la base de datos

Aquí defines la "forma" que tiene cada evento guardado: una tabla llamada demand_events con estas columnas:

id: un número que Postgres asigna solo, autoincremental (1, 2, 3...). Es el que el enunciado pide mostrar en /history/{id} — "el campo ID será el generado por ustedes".
idpk: el identificador que ya viene dentro del mensaje (un UUID). Se guarda aparte y se marca como unique, porque si el mismo mensaje te llega dos veces (pasa mucho con colas de mensajes, sobre todo cuando hay reconexiones) no quieres duplicar el registro.
type: el tipo de evento ("demand-set" por ahora).
package_body: todo el contenido del mensaje tal cual llegó, guardado como JSONB (un tipo especial de Postgres que guarda JSON pero permite además buscar dentro de él). La decisión de no "desarmar" cada campo del JSON en columnas separadas es deliberada: el enunciado dice que el contenido de los eventos "no es importante" por ahora, así que guardarlo completo tal cual es más simple y más a prueba de futuros cambios de formato.
valid_until y received_at: fechas, guardadas en columnas propias (aunque valid_until también está dentro del JSON) porque el enunciado pide poder filtrar "especialmente por lo relacionado a tiempo", y filtrar por una columna de fecha real es mucho más rápido y simple que buscar dentro de un JSON cada vez.

Si lo hicieras sola, esta sería la decisión no trivial más importante de todo el proyecto: ¿guardo cada campo del JSON en su propia columna, o guardo el JSON completo y solo saco afuera lo que necesito filtrar? La segunda opción (la que se usó) es la más simple para una entrega donde "el contenido no importa todavía".

* master/app/schemas.py — qué entra y qué sale de la API

Esto es distinto al archivo anterior: models.py describe la base de datos, schemas.py describe el JSON que la gente ve cuando le pega a tu API.

Hay dos formas (EventIn y EventOut) porque lo que entra no siempre es igual a lo que sale:

EventIn: lo que el connector te manda por POST. Son solo 4 campos (idpk, type, packageBody, receivedAt), porque eso es lo único que el connector necesita darte.
EventOut: lo que tu API responde. Acá viene el detalle más delicado del proyecto, que de hecho fue el que causó uno de los errores que tuviste: en la base de datos las columnas se llaman en formato snake_case (package_body, valid_until) porque así se acostumbra en Python, pero el enunciado usa camelCase (packageBody, validUntil) en sus ejemplos de JSON. Entonces cada campo lleva un "alias" que dice "esta columna de la base se llama así, pero muéstrala con este otro nombre en el JSON". Si no le hubieras puesto los alias (que fue justo el bug que tuviste), la API buscaría un campo que no existe con ese nombre exacto y fallaría.

Si lo hicieras tú sola, este es el típico error que se descubre recién probando el endpoint por primera vez — es difícil anticiparlo leyendo solo la documentación, se aprende chocando con él, tal como te pasó.

* master/app/main.py — la API en sí

Aquí se junta todo. Tiene 4 rutas:

GET /health: no la pide el enunciado como funcionalidad, pero sí pide que cada contenedor tenga un HEALTHCHECK. Docker necesita algo a lo que preguntarle "¿estás vivo?", y esta ruta responde justo eso. Es una ruta mínima, sin tocar la base de datos, para que responda rápido incluso si algo más anda lento.
POST /events: la usa el connector, nadie más. Recibe el evento, revisa si el idpk ya existía (para no duplicar), saca la fecha validUntil de adentro del JSON y la convierte a un formato de fecha real de Python, y guarda todo en la base.
GET /history: la lista paginada. page y limit controlan cuántos registros traer y desde dónde (el enunciado pide 25 por página por defecto). Los filtros (idpk, type, receivedAt, validUntil, city) son todos opcionales — si no mandas ninguno, te trae todo paginado. La razón por la que esto es importante: si no pagináramos, con miles de eventos la consulta se volvería lentísima y probablemente tumbaría el servicio, tal como advierte el enunciado.
GET /history/{event_id}: el detalle de un evento por su id. Si no existe, responde error 404 en vez de dejar caer el servidor.

Programándolo a mano, la parte más fácil de olvidar (y que de hecho tuviste que corregir) es la conexión entre el nombre broker.iic2173.org y el certificado SSL — pero esa parte vive en el connector, no acá. Dentro de este archivo, lo más fácil de pasar por alto es la paginación: es tentador hacer primero "tráeme todo" y agregar la paginación después, pero como el enunciado lo marca como Esencial, конviene pensarlo desde el diseño de la consulta, no como un parche al final.

* master/Dockerfile — cómo empaquetar el master

Un Dockerfile es como una receta de cocina: dice paso a paso cómo armar la "caja" (imagen) donde va a vivir tu programa. Line por línea:

Parte de una imagen base de Python ya instalada (python:3.11-slim), para no tener que instalar Python desde cero.
Instala curl, que no lo necesita tu código, sino el HEALTHCHECK de más abajo (que usa curl para preguntarle a /health si todo anda bien).
Copia el requirements.txt primero, instala las librerías, y recién después copia el resto del código. Este orden no es casualidad: Docker reutiliza pasos que no cambiaron entre una construcción y otra, entonces si solo cambias tu código (y no tus librerías), Docker no reinstala todo de nuevo — la construcción es mucho más rápida.
El HEALTHCHECK le pregunta a /health cada 30 segundos.
La última línea prende el servidor (uvicorn), que es el programa que realmente sirve tu API por HTTP.
connector/connector.py — el programa que escucha RabbitMQ

Este es el archivo con más decisiones no triviales, porque el enunciado exige explícitamente que resista caídas sin intervención manual. Por partes:

Credenciales por variables de entorno: igual que con la base de datos, nunca se escriben usuario/contraseña directo en el código. Se leen del archivo .env, que además nunca se sube a Github (por eso existe el .gitignore).
touch_heartbeat(): corre en un hilo aparte (threading) y cada 15 segundos escribe la hora actual en un archivo. Esto existe únicamente porque el enunciado permite que el HEALTHCHECK del connector sea "por archivo" en vez de por HTTP (dice explícitamente que no es necesario que el connector tenga su propia API). Entonces en vez de montar un servidor web solo para decir "estoy vivo", basta con este archivito que se va actualizando solo.
send_to_master(): arma el mensaje y lo manda por POST al master. Nótese que acá se pone receivedAt con la hora actual — es el connector, no el broker, quien decide cuándo "se recibió" el evento, porque es el que efectivamente lo recibió.
on_message(): esta es la función que se ejecuta cada vez que llega un mensaje nuevo. Si todo sale bien, confirma la recepción a RabbitMQ (basic_ack) — eso le dice a RabbitMQ "ya lo procesé, bórralo de la cola". Si algo falla (por ejemplo, el master está caído en ese momento), en vez de confirmar, hace basic_nack con requeue=True, que le dice a RabbitMQ "no pude, devuélvelo a la cola para reintentarlo más tarde". Esta es la pieza clave que cumple el requisito de "si se pierde la conexión con el broker, no debe perderse el mensaje".
connect_and_consume(): arma la conexión, usando SSL porque el broker exige TLS (puerto 5671). Un detalle importante y fácil de pasar por alto: se usa ssl.create_default_context(), que carga automáticamente los certificados confiables del sistema operativo. Si en cambio hubieras creado el contexto SSL "pelado" (ssl.SSLContext(...) sin más), Python no sabría en quién confiar y la conexión fallaría con el error de certificado que tuviste — ese fue justamente el bug que corregimos juntos.
main(): el while True de afuera es la verdadera resiliencia. Si connect_and_consume() se cae por cualquier motivo (el broker se reinicia, se corta la red, etc.), el error se atrapa, se espera 5 segundos, y se vuelve a intentar — para siempre, sin que nadie tenga que reiniciar el contenedor a mano.

Si lo hicieras tú sola sin haber chocado antes con estos problemas, lo más probable es que el primer intento no tuviera el try/except alrededor de la conexión (se cae la primera vez que el broker tenga un hipo) y que el contexto SSL fallara igual que te pasó a ti — son errores clásicos que casi todos cometen la primera vez que usan colas de mensajes con TLS.

* connector/Dockerfile — cómo empaquetar el connector

Muy parecido al del master, con dos diferencias:

Instala ca-certificates explícitamente. En rigor, como ya arreglamos el código con create_default_context(), ya no era estrictamente necesario — pero no está de más tenerlo, por si algún día se usa una imagen base más pelada que no traiga certificados instalados.
El HEALTHCHECK no usa curl contra una URL, sino el comando find para revisar si el archivo de heartbeat se modificó en el último minuto. Si touch_heartbeat() dejó de correr (por ejemplo, el programa se colgó), el archivo deja de actualizarse, find no encuentra nada reciente, y Docker marca el contenedor como no saludable.
docker-compose.yml — el director de orquesta

Este archivo no tiene código de tu proyecto, pero es el que hace que los tres pedazos (base de datos, master, connector) funcionen juntos como si fueran uno solo. Decisiones importantes acá:

db primero, con condition: service_healthy: master depende de que la base de datos ya esté lista antes de arrancar (si no, se caería al intentar conectarse a una base que todavía no existe).
Una red (energyshark_net) compartida: así los contenedores se pueden llamar entre sí por nombre (db, master) en vez de por IP, que además cambia cada vez que se reinician.
Un volumen (db_data): sin esto, cada vez que apagas y prendes los contenedores perderías todos los datos guardados, porque por defecto el contenido de un contenedor se borra al eliminarlo. El volumen vive fuera del contenedor y sobrevive a los reinicios.
env_file: .env en el connector: así no hace falta repetir las credenciales dentro del propio docker-compose.yml (que si se sube a Github, sería un problema).

Armar este archivo bien suele ser iterativo: normalmente se descubre que falta el depends_on o el healthcheck recién cuando algo falla en un orden inesperado al levantar todo por primera vez — no es un archivo que se escribe perfecto a la primera sin antes ver un par de errores de "no se pudo conectar a la base".

* .env.example, .gitignore y README.md

Los tres son de "higiene" del proyecto más que de funcionalidad:

.env.example es una plantilla sin datos reales, para que cualquiera (tú en otro computador, un ayudante revisando) sepa qué variables necesita sin exponer tu contraseña real.
.gitignore le dice a Git qué archivos nunca debe subir — clave para no subir por accidente el .env con tu contraseña real, ni el .pem de AWS (que el enunciado prohíbe explícitamente y castiga con nota 1 si se sube).
README.md es simplemente la checklist que el enunciado pide entregar, con espacio para que rellenes tú qué lograste y qué no.
