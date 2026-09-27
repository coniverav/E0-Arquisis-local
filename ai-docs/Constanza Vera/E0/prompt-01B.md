## Prompt

Adjunté el enunciado de la Entrega 0 (Enunciado_E0_v1.pdf) y pedí:

"Dime paso a paso cómo hacer esto, sé detallado y dame los códigos necesarios
para completar la tarea correctamente. Las credenciales déjalas con un
comentario de lo que debería poner ahí. Explícame dónde debo programar y cómo
se deben llamar los archivos (quiero todo el detalle posible). Primero dámelo
para correrlo en local, luego me das las instrucciones para subirlo a AWS. Sé
conciso y no uses lenguaje técnico. Que sea en python, con FastAPI"

## Respuesta

Voy a preparar el proyecto completo. Dame un minuto para armar los archivos.

Se crearon 21 archivos, se ejecutaron 4 comandos, editó 2 archivos · 6 notas

Cómo está organizado

Descomprime el zip. Vas a tener dos programas separados, tal como pide el enunciado:

* master/ → tu API (FastAPI). Aquí guardas y muestras los datos. Todo el código va en master/app/.
* connector/ → el programa que escucha RabbitMQ y le avisa a master. Es un solo archivo: connector/connector.py.
* docker-compose.yml → levanta los tres servicios juntos (master, connector, base de datos).
* .env.example → aquí van tus credenciales del broker. Cópialo como .env y rellénalo (ese archivo nunca se sube a Github).

No tienes que crear archivos nuevos para que funcione la Entrega 0 mínima; si más adelante quieres agregar algo, los endpoints van en master/app/main.py y la lógica de conexión al broker en connector/connector.py.

1. Copia y completa el .env
Dentro de la carpeta energyshark-e0, copia .env.example a .env (comando: cp .env.example .env). Ábrelo y reemplaza RABBITMQ_USER, RABBITMQ_PASS y QUEUE_NAME con los datos que te llegaron por Canvas.
2. Instala Docker Desktop
Si no lo tienes, descárgalo e instálalo. Es lo único que necesitas para probar todo en tu computador, no necesitas instalar Python ni Postgres por separado.
3. Levanta todo con un comando
Abre una terminal en la carpeta energyshark-e0 y ejecuta: docker compose up --build. Esto construye y prende los tres servicios (base de datos, master y connector) y muestra los logs en pantalla.
4. Revisa que esté vivo
En el navegador entra a http://localhost:8000/health, debe responder {"status":"ok"}. En la terminal deberías ver líneas del connector diciendo que se conectó y que va guardando eventos a medida que lleguen (el broker recién se activa el lunes, antes de eso no vas a ver eventos).
5. Prueba los endpoints
http://localhost:8000/history muestra la lista paginada. http://localhost:8000/history/1 muestra el detalle del primer evento. Puedes agregar ?page=2&limit=10 o ?receivedAt=2026-08-24 a la URL para probar los filtros.
6. Apaga cuando termines
Con Ctrl+C detienes los logs, y docker compose down apaga y elimina los contenedores (los datos de la base quedan guardados para la próxima vez que los prendas).

Cuando eso funcione, aquí está lo mismo pero en la nube:

1. Crea la máquina en AWS
Entra a EC2 en la consola de AWS y crea una instancia "t2.micro" o "t3.micro" (son las gratis), con Ubuntu como sistema operativo. Al crearla, descarga el archivo .pem: es tu llave para entrar por SSH, guárdala bien y no la subas nunca a Github.
2. Abre los puertos necesarios
En la configuración de "Security Group" de la instancia, permite entrada en el puerto 22 (SSH), 80 (HTTP) y 443 (HTTPS). El puerto 8000 no hace falta abrirlo al mundo, solo lo usa Nginx internamente.
3. Conéctate por SSH
Desde tu terminal: ssh -i tu-llave.pem ubuntu@IP_DE_TU_INSTANCIA. La IP la ves en la consola de EC2. Si te da error de permisos en el .pem, ejecuta antes: chmod 400 tu-llave.pem
4. Instala Docker en la máquina
Ya conectado por SSH, instala Docker Engine y el plugin de compose siguiendo la guía oficial de docker.com para Ubuntu. Al final agrega tu usuario al grupo docker para no tener que usar sudo siempre.
5. Sube tu código
La forma más simple: sube tu proyecto a un repositorio de Github (sin el .env ni el .pem, gracias al .gitignore) y luego en la instancia haces git clone. Ahí dentro creas el archivo .env con tus credenciales reales (nunca lo subas al repo).
6. Levanta los contenedores
Dentro de la carpeta del proyecto en la máquina EC2, ejecuta: docker compose up --build -d (el -d lo deja corriendo en segundo plano aunque cierres la sesión SSH).
7. Configura el dominio
En Namecheap (u otro proveedor), crea un registro DNS tipo A que apunte tu dominio a la IP pública de tu instancia EC2. Puede tardar unos minutos en propagarse.
8. Instala Nginx como proxy inverso
Instala Nginx directamente en la máquina (no en un contenedor): sudo apt install nginx. Configura un archivo en /etc/nginx/sites-available/ que reciba las peticiones a tu dominio y las redirija a http://localhost:8000 (donde escucha master). Actívalo con un enlace en sites-enabled y reinicia Nginx.

Un par de cosas importantes que quedan pendientes de tu parte, no del código:

* HTTPS y balanceo de carga son la parte variable (25%), no los incluí porque tienes que elegir cuál de las dos hacer. Si quieres, te armo esa parte después.
* Registrar los prompts: el enunciado pide guardar cada consulta a Claude en aidocs/prompts. Como usaste esta conversación para generar el código, te conviene copiar este prompt y mi respuesta ahí antes de entregar.
* El broker recién se activa el lunes, así que hasta entonces no vas a ver eventos reales llegando — pero puedes probar el POST /events a mano (por ejemplo con curl) para confirmar que master guarda y muestra bien los datos.