## Prompt

Pegué las credenciales reales entregadas por Canvas para mi observer (host,
puerto, virtual host "energy", usuario "observer.53", contraseña, cola
"observer.53.q", exchange, y las notas sobre el certificado TLS y los
permisos de la cola) y pregunté:

"dime exáctamente cómo deben estar los .env y si hay algo más que modificar"

## Respuesta

Sí, falta el virtual host (energy) que traen tus credenciales — el código original no lo tenía. Lo agrego:

Editó 2 archivos, ejecutó un comando · 1 nota

[contenido .env]

Copia eso, guárdalo como archivo .env (no .env.example) dentro de energyshark-e0/.

Editó un archivo, ejecutó un comando, archivo leído

Dos cosas más que valen la pena saber de esas credenciales:

* observer.broadcast: el enunciado de Canvas dice que además de tu cola personal, te pueden llegar mensajes de difusión general. No tienes que hacer nada especial en el código — la cola observer.53.q ya está configurada por el servidor para recibir ambos tipos, tú solo consumes de ella como está.
* No borres el .env.example del zip: úsalo solo de plantilla. El archivo real con tu contraseña debe llamarse .env y ya está en el .gitignore, así que no se subirá a Github por accidente.

Con eso, corre de nuevo docker compose up --build y deberías empezar a ver en los logs líneas como [connector] evento ... guardado en master apenas lleguen mensajes.