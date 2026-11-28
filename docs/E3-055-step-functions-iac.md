# Despliegue de sagas en AWS

El despliegue objetivo es exclusivamente `EnergySharkProd-Sagas`. No se deben
desplegar `ExistingInfrastructure` ni `Monitoring`: PostgreSQL, EC2 y los
servicios E2 existentes permanecen intactos. Los feature flags de seller y
buyer parten desactivados.

## Red privada

Las Lambdas se conectan a la VPC y subred de la EC2 existente mediante un
security group propio. Este grupo solo puede abrir conexiones hacia el security
group dedicado de los proxies en los puertos:

- `15432`: HAProxy TCP hacia `db:5432` en la red privada de Docker.
- `3128`: Squid CONNECT con destinos limitados a Wallet, Secrets Manager y
  Step Functions en `us-east-2`.

Los puertos se publican únicamente en la IP privada de EC2 indicada por
`E3_EC2_PRIVATE_IP`. Su valor seguro por defecto es `127.0.0.1`, por lo que una
configuración incompleta no los expone. PostgreSQL conserva su binding actual
en `127.0.0.1:5433`.

## Secretos

CDK crea `EnergySharkProd-Sagas-Database` y
`EnergySharkProd-Sagas-Wallet` con valores iniciales no sensibles y política
`RETAIN`. El prefijo cambia con el ambiente. Los valores reales se cargan
después por un canal operativo fuera de Git, Compose, logs y CloudFormation. No
se entregan secretos mediante parámetros de stack. Las Lambdas reciben
solamente los ARN y tienen permiso de lectura sobre esos dos recursos.

El secreto de base acepta `DATABASE_URL` apuntando a `<ip-privada>:15432`. El
de Wallet acepta `baseUrl`, `apiKey`, `jwksUrl` e `issuer`; `baseUrl` conserva
`/v1` y JWKS permanece en la raíz oficial. Los JSON reales se preparan en una
ruta privada fuera del repositorio y se cargan con `put-secret-value` usando los
ARN de los outputs, sin escribir su contenido en la línea de comandos.

## Validación local

```bash
cd infra/cdk
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r lambda_src/saga_task/requirements.txt
npm install --prefix .venv/cli --no-audit --no-fund aws-cdk@2.1149.0
JSII_RUNTIME_PACKAGE_CACHE_ROOT=/tmp/jsii-cache \
  .venv/bin/python -m unittest discover -s tests -v

CDK_DEFAULT_ACCOUNT=111122223333 CDK_DEFAULT_REGION=us-east-2 \
AWS_DEFAULT_REGION=us-east-2 AWS_EC2_METADATA_DISABLED=true \
JSII_RUNTIME_PACKAGE_CACHE_ROOT=/tmp/jsii-cache \
  .venv/cli/node_modules/.bin/cdk synth EnergySharkProd-Sagas \
  -c environment=prod -c deploymentScope=sagas --exclusively --no-lookups \
  --quiet --app '.venv/bin/python app.py' -o /tmp/e3-sagas
```

Antes de un despliegue autorizado hay que ejecutar `cdk bootstrap` si la cuenta
no tiene `CDKToolkit`, cargar ambos secretos, instalar los proxies y comprobar
conectividad desde una Lambda de prueba. El despliegue debe nombrar únicamente
`EnergySharkProd-Sagas`, usar `-c environment=prod --exclusively` y proporcionar
`-c deploymentScope=sagas`, además de proporcionar los parámetros de VPC,
subred e IP privada. Después se asocia a EC2 el security group identificado por
el output `SagaProxySecurityGroupId`; no se modifica el grupo existente.

## Orden manual futuro

1. Verificar cuenta/región y ejecutar `cdk bootstrap aws://<cuenta>/us-east-2`.
2. Sintetizar y revisar el diff solo con `environment=prod` y
   `deploymentScope=sagas`.
3. Desplegar únicamente `EnergySharkProd-Sagas` con los parámetros
   `ExistingVpcId`, `ExistingSubnetId` y `ApplicationPrivateIp`.
4. Cargar los dos secretos retenidos y asociar a EC2 el SG de proxies y el
   instance profile, preservando cualquier permiso operativo existente.
5. Definir `E3_EC2_PRIVATE_IP`, levantar solo ambos proxies y validar rutas.
6. Copiar los ARN seller/buyer a Compose, todavía con ambos flags en `false`.
7. Ejecutar una prueba controlada antes de habilitar seller y buyer por separado.

## Rollback

Mantener ambos flags en `false` durante instalación y validación. Ante un fallo,
desactivarlos, detener los proxies y retirar la asociación IAM nueva. No se
elimina el stack mientras existan ejecuciones y no se recrea ni restaura la base
de datos. Los secretos retenidos sobreviven a un eventual retiro del stack.

### Recuperación del primer despliegue fallido

Un stack en `ROLLBACK_COMPLETE` solo admite eliminación. Antes de reintentar:

1. Publicar como versiones predeterminadas las policies corregidas
   `EnergySharkSagasCfnExecution-us-east-2` y
   `EnergySharkCdkBootstrapBoundary-us-east-2`.
2. Guardar eventos, recursos y los 15 nombres físicos de log groups como
   evidencia. `DELETE_SKIPPED` confirma la retención.
3. Verificar por nombre exacto que los dos secretos no existan. Si alguno
   existe, no eliminarlo: revisar su valor y adoptar/importar el recurso antes
   de otro despliegue.
4. Desactivar temporalmente termination protection y eliminar únicamente el
   stack fallido. La boundary permite `DeleteStack` solo para
   `EnergySharkProd-Sagas`.
5. Confirmar `DELETE_COMPLETE`; los log groups retenidos quedarán huérfanos y
   no deben borrarse hasta conservar su evidencia y completar un despliegue
   nuevo exitoso.
6. Repetir synth/diff y desplegar con la VPC
   `vpc-0e7ee77fd065bea62`. Mantener los feature flags apagados.

Los secretos con `DELETE_SKIPPED` pero sin identificador físico no prueban que
exista un recurso administrable. Si la consulta por los dos nombres exactos no
encuentra secretos, no requieren limpieza. Cualquier borrado de log groups
retenidos es una acción manual posterior, usando la lista exacta del intento
fallido.
