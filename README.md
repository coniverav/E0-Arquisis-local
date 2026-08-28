# EnergyShark - Entrega 0 ⚡

## Acceso a la API
- Dominios: coniverav.tech www.coniverav.tech
- Historial: https://coniverav.tech/history
- Swagger/documentación: https://coniverav.tech/docs
- Health master: https://coniverav.tech/health
- IP elástica EC2: 18.116.213.143

## Acceso al servidor
```cmd
ssh -i "arquisis-keys.pem" ubuntu@ec2-18-116-213-143.us-east-2.compute.amazonaws.com
```

## Puntos logrados

### Requisitos funcionales (10pts):
- [x] RF1 [escencial] - historial completo (paginado).
- [x] RF2 - detalle por ID.
- [x] RF3 [escencial] - paginación con queryParams
- [x] RF4 [escencial] - filtros por propiedad

### Requisitos no funcionales: (20pts)
- [x] RNF1 [escencial] - connector con reconexión: connector usa `aio-pika` con reconexión automática, consume observer.53.q y envía cada evento con httpx por POST a master, que lo guarda en PostgreSQL.
- [x] RNF2 [escencial] - despliegue containerizado: master y connector comparten la misma red `red Docker energyshark_net`.
- [x] RNF3 - proxy inverso en EC2 (Nginx): configurado en `nginx/energyshark.conf`.
- [x] RNF4 - dominio propio: `coniverav.tech`.
- [x] RNF5 [escencial] - ejecutando en EC2 free tier.
- [x] RNF6 - base de datos externa Postgres
- [x] RNF7 [escencial] - HEALTHCHECK: estos se comprueban al momento de hacer `sudo docker compose ps`.

### Docker compose (15pts):
- [x] RNF1 - master(s) desde docker-compose
- [x] RNF2 - DB desde docker-compose
- [x] RNF3 - connector desde docker-compose y conectado al contendor de la app web

### Variable (25%):

- #### HTTPS (15pts):
    - [x] RNF1 - SSL con Let's Encrypt
    - [x] RNF2 - redirección de HTTP a HTTPS
    - [x] RNF3 - chequeo de expiración del certificado SSL automática 2 veces al día

- #### Balanceo de carga con Nginx (15pts):
    - [x] RF1 - replicar master en 2 instancias container paralelas
    - [x] RF2 - instancias master alcanzables desde el Nginx


## Consideraciones generales para comprobar algunos ítems

* ### HEALTHCHECKS

Para comprobar los estados se hace en la carpeta del proyecto:

```bash
sudo docker compose ps
```

Donde aparecen `db`, `master`, `master2` y `connector` en ejecución y tras unos segundos los healthchecks de `healthy`.


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

Al acceder reiteradas veces a `https://www.coniverav.tech/health`, la instancia cambia entre master y master2.