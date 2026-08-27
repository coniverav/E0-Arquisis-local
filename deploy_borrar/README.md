# Archivos preparados para las dos partes variables

Estos archivos **no se ejecutan en local hasta la etapa 12**. Se usarán cuando exista EC2 + Elastic IP + dominio.

- `nginx/energyshark-http.conf.template`: reverse proxy y round-robin hacia `master` (8001) y `master2` (8002).
- `nginx/energyshark-https.conf.template`: misma arquitectura, pero con HTTPS y redirección HTTP→HTTPS.
- `setup_host_nginx_https.sh`: instala Nginx/Certbot en el **host EC2**, obtiene Let's Encrypt y crea un cron de renovación a las 00:00 y 12:00.

Esto respeta el requisito de que Nginx esté directamente sobre EC2 y no en un contenedor.
