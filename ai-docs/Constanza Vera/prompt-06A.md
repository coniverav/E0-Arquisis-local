## Prompt

Cómo puedo hacer el chequeo de expiración del certificado SSL automática 2 veces al día?

## Respuesta

Para tu tarea, la solución más simple es usar cron y ejecutar el chequeo dos veces al día.

Ejecuta:
```
sudo crontab -e
```
Si te pregunta qué editor usar, selecciona nano.

Al final agrega:
```
0 0,12 * * * /usr/local/bin/certbot renew --quiet --deploy-hook "systemctl reload nginx"
```
Eso significa:
```
00:00 → certbot renew
12:00 → certbot renew
```
certbot renew no renueva el certificado cada vez; solamente comprueba si está suficientemente cerca de expirar y lo renueva cuando corresponde.

El deploy-hook:
```
systemctl reload nginx
```
hace que, si realmente se renueva el certificado, Nginx cargue el nuevo.

Comprueba que quedó guardado
```
sudo crontab -l
```
Deberías ver:
```
0 0,12 * * * /usr/local/bin/certbot renew --quiet --deploy-hook "systemctl reload nginx"
```
Y prueba Certbot manualmente:
```
sudo /usr/local/bin/certbot renew --dry-run
```
Si el dry-run es exitoso, tu RNF queda conceptualmente:
```
Certificado Let's Encrypt ✅
HTTP → HTTPS ✅
Chequeo automático 2 veces al día ✅
Renovación solo si corresponde ✅
Nginx recarga el certificado nuevo ✅
```