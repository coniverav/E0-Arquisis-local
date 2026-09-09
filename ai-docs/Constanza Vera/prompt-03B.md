## Prompt

"ejecuto eso en cmd o ubuntu?"

## Respuesta

Depende de cómo tengas instalado Docker:

Si usas Docker Desktop en Windows (lo más común): puedes ejecutarlo directo en CMD o PowerShell, sin necesidad de Ubuntu/WSL. Docker Desktop ya deja el comando docker disponible ahí.
Si tienes WSL con Ubuntu instalado y ahí configuraste Docker: entonces ejecútalo dentro de esa terminal Ubuntu.

Para saber cuál te corresponde, abre CMD y escribe:

docker --version

Si te responde con la versión (algo como Docker version 27...), estás listo para correrlo ahí mismo, en la carpeta donde descomprimiste el proyecto. Si dice que no reconoce el comando, entonces necesitas usar la terminal de Ubuntu (o instalar Docker Desktop primero).