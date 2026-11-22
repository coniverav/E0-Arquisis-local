# Artefacto de tareas de saga

CDK empaqueta este directorio como una imagen común e inmutable para las
tareas. `seller-proposal`, `buyer-acceptance`, reconciliación y cancelación
usan Wallet con secretos de Secrets Manager y cuota PostgreSQL compartida.
Los callbacks registran el task token antes de cualquier llamada HTTP. El
resto de tareas sigue fallando de forma explícita hasta su implementación.

El secret Wallet contiene `baseUrl` HTTPS y `apiKey`; el de base de datos usa
`DATABASE_URL` o los campos `host`, `port`, `username`, `password` y `dbname`.
Nunca se registran secretos, cuerpos completos, task tokens, JWT ni
`swapToken`. La imagen fija la versión de `psycopg-binary`; no se despliega en
esta rama.
