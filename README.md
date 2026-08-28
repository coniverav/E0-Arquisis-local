# EnergyShark - Entrega 0 ⚡

Es importante mencionar que el archivo `.env` real se encuentra configurado directamente en la instancia EC2
y no se versiona en GitHub por contener credenciales privadas. En este repositorio se incluye `.env.example` como referencia.

## Acceso a la API
- Dominios: coniverav.tech www.coniverav.tech
- Historial: https://coniverav.tech/history
- Swagger/documentación: https://coniverav.tech/docs
- Health API/backend: https://coniverav.tech/health
- IP elástica EC2: 18.116.213.143

## Acceso al servidor
```cmd
ssh -i "arquisis-keys.pem" ubuntu@ec2-18-116-213-143.us-east-2.compute.amazonaws.com
```

## Puntos logrados

### Requisitos funcionales (10pts):
- [x] RF1 [esencial] - historial completo de demandas recibidas.
- [x] RF2 - detalle por ID mediante `/history/{id}`.
- [x] RF3 [esencial] - paginación mediante queryParams `page` y `limit`, con 25 registros por defecto.
- [x] RF4 [esencial] - filtros por propiedad.

### Requisitos no funcionales: (20pts)
- [x] RNF1 [esencial] - connector con reconexión: connector usa `aio-pika` con reconexión automática, consume `observer.53.q` y envía cada evento con `httpx` por POST a master, que lo guarda en PostgreSQL.
- [x] RNF2 [esencial] - despliegue containerizado: master y connector comparten la misma red Docker `energyshark_net`.
- [x] RNF3 - proxy inverso en EC2 con Nginx, configurado en `nginx/energyshark.conf`.
- [x] RNF4 - dominio propio: `coniverav.tech`.
- [x] RNF5 [esencial] - aplicación ejecutándose en una instancia AWS EC2 free tier.
- [x] RNF6 - base de datos PostgreSQL ejecutándose como servicio independiente.
- [x] RNF7 [esencial] - todos los containers poseen HEALTHCHECK y su estado puede verificarse mediante `sudo docker compose ps`.

### Docker compose (15pts):
- [x] RNF1 - master(s) levantados desde Docker Compose.
- [x] RNF2 - base de datos integrada desde Docker Compose.
- [x] RNF3 - connector levantado desde Docker Compose y conectado al container de la aplicación web.

### Variable (25%) [ambas realizadas]:

- #### HTTPS (15pts):
    - [x] RNF1 - SSL con Let's Encrypt.
    - [x] RNF2 - redirección automática de HTTP a HTTPS.
    - [x] RNF3 - chequeo automático de expiración del certificado SSL dos veces al día.

- #### Balanceo de carga con Nginx (15pts):
    - [x] RF1 - master replicado en dos instancias container ejecutándose en paralelo.
    - [x] RF2 - ambas instancias master son alcanzables desde Nginx y participan del balanceo de carga.


## Consideraciones generales para comprobar algunos ítems

* ### HEALTHCHECKS

Para comprobar el estado de los containers, ejecutar desde la carpeta del proyecto:

```
sudo docker compose ps
```

Deben aparecer `db`, `master`, `master2` y `connector` en ejecución con estado `healthy`.

* ### Renovación automática SSL

La expiración de los certificados Let's Encrypt se verifica automáticamente dos veces al día (a mediodía y a medianoche) mediante cron, haciendo la renovación solo si corresponde.

Ver configuración:
```
sudo crontab -l
```
Probar renovación:
```
sudo /usr/local/bin/certbot renew --dry-run
```

* ### Instancias master paralelas y alcanzables

Para comprobar el balanceo se puede ejecutar reiteradas veces:

```
curl https://coniverav.tech/health
```

La respuesta indica la instancia que atendió la solicitud:
```
{"status":"ok","instance":"master"}
```
o:
```
{"status":"ok","instance":"master2"}
```
Esto permite comprobar que Nginx distribuye solicitudes entre ambas instancias.