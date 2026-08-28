## Prompt

cómo modifico el yml para poder activar la actualización automática? (docker compose up --watch)

## Respuesta

--watch es una función de Docker Compose que sincroniza tus cambios de código al contenedor sin que tengas que reconstruir todo a mano. Se configura agregando un bloque develop: watch: a cada servicio, diciéndole qué carpeta vigilar y qué hacer cuando cambia.

Editó 2 archivos, archivo visualizado

Cómo queda cada servicio:

master: usa action: sync — copia tus cambios de master/app al contenedor al vuelo. Le agregué --reload a uvicorn (el servidor web) para que, apenas detecte el archivo copiado, se reinicie solo. Si tocas requirements.txt (agregas una librería nueva), ahí sí reconstruye la imagen completa, porque eso no se puede "copiar en caliente".
connector: como es un script simple sin recarga automática, usa action: sync+restart — copia el archivo y reinicia el contenedor completo. Igual que en master, si cambia requirements.txt reconstruye la imagen.

Para usarlo, en vez de docker compose up --build corres:

docker compose up --watch

Y desde ahí, cada vez que guardes un cambio en master/app/*.py o en connector/connector.py, se aplica solo, sin que tengas que parar y volver a levantar todo a mano.

Ejecutó un comando, archivo leído

Un detalle: --watch necesita una versión relativamente reciente de Docker Compose (v2.22 o superior). Si te tira error de que no reconoce la opción, corre docker compose version para confirmar cuál tienes.