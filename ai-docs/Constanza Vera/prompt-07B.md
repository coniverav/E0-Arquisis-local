## Prompt

Pegué el traceback del master mostrando `fastapi.exceptions.ResponseValidationError`
con "Field required" para packageBody, validUntil y receivedAt, y el connector
reintentando sin parar por los 500. Dije: "Falló rotúndamente:"

## Respuesta

Buena noticia: el connector ya se conectó bien y está mandando datos — el error ahora es de mi código en el master, en cómo arma la respuesta. Lo arreglo:

Archivo visualizado, editó un archivo

Qué pasaba: en la base de datos guardé las columnas como package_body, valid_until, received_at (estilo Python), pero le pedía a la API que las mostrara como packageBody, validUntil, receivedAt (estilo JSON) sin decirle que son lo mismo. Por eso decía "Field required": buscaba packageBody y solo encontraba package_body.

Ejecutó un comando, archivo leído

Reemplaza tu master/app/schemas.py y corre:

docker compose up --build master

Un detalle: como el connector estuvo fallando un rato, seguramente reintentó los mismos mensajes muchas veces (por eso viste tantas líneas repetidas — eso es RabbitMQ reencolando por el nack). No es un bug, es justamente la resiliencia que pide el enunciado (RNF1) funcionando bien. Una vez que master vuelva a levantar, deberían empezar a guardarse correctamente y /history va a mostrar datos.