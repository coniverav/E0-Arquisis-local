# Integración de EC2 con Step Functions

`EnergySharkProd-Sagas` declara un rol y un instance profile para el Connector.
La plantilla no los asocia automáticamente a la EC2 existente: hacerlo sin
conocer su perfil actual podría retirar permisos operativos de E2.

## Permisos mínimos

El rol permite:

- `states:StartExecution` sobre los alias seller y buyer del stack.
- `states:DescribeExecution` sobre ejecuciones de esas máquinas.
- `states:SendTaskSuccess` para callbacks con task token; AWS no admite limitar
  esta acción a un ARN concreto.

No incluye permisos administrativos, claves estáticas ni acceso a secretos. El
Connector obtiene credenciales temporales mediante el instance profile.

## Preparación del host

1. Inventariar el perfil y los permisos actualmente asociados a EC2.
2. Conservar esos permisos en un rol compuesto o adjuntar la política mínima al
   rol operativo antes de reemplazar la asociación.
3. Exigir IMDSv2 y configurar `HttpPutResponseHopLimit=2` para que los
   contenedores Docker puedan obtener credenciales; validar primero en pruebas.
4. Configurar en `connector` los ARN seller/buyer producidos por el stack.
5. Configurar los mismos ARN en `master`, `master2` y `cycle-scheduler` cuando
   corresponda; mantener `E3_SELLER_SAGA_ENABLED=false` y
   `E3_BUYER_SAGA_ENABLED=false` hasta finalizar la validación.

Compose declara los proxies de PostgreSQL y HTTPS como servicios adicionales.
Ambos escuchan solo en la IP privada indicada. Antes de iniciarlos se asocia a
la EC2 el security group dedicado entregado por Sagas; este admite únicamente
el security group de Lambda en ambos puertos. Squid restringe los destinos
permitidos y HAProxy solo reenvía al servicio `db`.

## Validación y activación

Verificar desde el contenedor Connector que boto3 obtiene una identidad temporal
sin variables `AWS_ACCESS_KEY_ID`, y que puede iniciar únicamente las máquinas
propias. Desde una Lambda de prueba comprobar lectura de secretos, conexión a
PostgreSQL mediante `15432`, Wallet/JWKS mediante `3128` y ausencia de salida a
destinos no autorizados. Luego habilitar un rol a la vez y observar logs,
callbacks, outbox y ledger.

## Rollback

Desactivar los flags antes de cambiar IAM. Si falla la obtención de credenciales
o el inicio de sagas, restaurar la asociación original del instance profile y
detener los relays E3. Los servicios E2 continúan operando porque los flags son
opt-in y PostgreSQL conserva su volumen, puerto y red actuales.
