# IaC de EnergyShark

Se usa AWS CDK v2 con Python. El stack E2 ya declara red, cómputo, frontend, API pública y observabilidad reproducibles en una cuenta limpia (E3-076). El stack de sagas declara la parte serverless E3, pero todavía no se ha desplegado.

## Estructura

- `app.py`: aplicación CDK y configuración común.
- `energyshark_infra/config.py`: ambientes y nombres de stacks.
- `api_routes.py`: tabla versionada de las 29 rutas públicas del API.
- `stacks/existing_infrastructure.py`: red, cómputo, bucket, CloudFront y API HTTP.
- `stacks/sagas.py`: Step Functions Standard, Lambdas, logs e IAM.
- `asl/`: definiciones seller/buyer versionadas.
- `lambda_src/saga_task/`: imagen común reproducible para tareas de saga.
- `stacks/monitoring.py`: alarmas CloudWatch, topic SNS y panel de operación.
- `tests/`: valida límites, composición de cada stack y ausencia de secretos.

## Estado y ambientes

Los recursos E2 se crean desde cero, sin importar nada. El stack `ExistingInfrastructure` declara:

- VPC propia (`networkCidr`, por defecto `10.20.0.0/16`) con tres subredes públicas, internet gateway y sin NAT.
- Instancia `t3.micro` con IP elástica, volumen raíz cifrado de 20 GiB, IMDSv2 obligatorio y rol con `AmazonSSMManagedInstanceCore` y `CloudWatchAgentServerPolicy`.
- Bucket privado del frontend con cifrado AES256, TLS obligatorio y las cuatro opciones de bloqueo público en `true`.
- CloudFront con Origin Access Control, `index.html` por defecto y respuestas 403/404 reescritas a `index.html` con código 200.
- API Gateway HTTP con las 29 rutas de `api_routes.py`, autorizador JWT de Auth0 y logs de acceso.

El stack `Monitoring` añade cinco alarmas (5xx, latencia p95, 4xx, estado de la instancia y CPU), un topic SNS con suscripción opcional por correo y un panel de operación.

El stack de sagas declara dos máquinas Standard, una versión y un alias `live` por máquina, 13 Lambdas y sus roles. Los handlers cubren reservas, propuestas, validación, callbacks, reconciliación y cierre. El procedimiento de E3-055 permite revisar el cambio antes de activar la integración de E3-056. [Guía de despliegue de sagas](../../docs/E3-055-step-functions-iac.md).

`dev`, `staging` y `prod` parametrizan nombres, timeout total, memoria, timeout Lambda y retención de logs. Cuenta y región provienen de `CDK_DEFAULT_ACCOUNT` y `CDK_DEFAULT_REGION`; producción activa protección contra eliminación.

Claves de contexto opcionales del stack E2 (todas con valor por defecto):

| Clave | Uso |
| :--- | :--- |
| `networkCidr` | CIDR de la VPC nueva |
| `instanceType` | Tipo de la instancia de aplicación |
| `amiIds` | Mapa región a AMI; por defecto `us-east-2` |
| `keyName` | Par de llaves existente para SSH |
| `adminCidr` | CIDR con acceso SSH; vacío usa solo Session Manager |
| `frontendBucketName` | Nombre del bucket del frontend |
| `frontendDomainName` | Alias de CloudFront |
| `certificateArn` | Certificado ACM en `us-east-1` para el alias |
| `apiBackendUrl` | Base del backend detrás del API |
| `jwtIssuer`, `jwtAudience` | Autorizador JWT |
| `corsOrigins` | Orígenes permitidos por CORS |
| `appRepositoryUrl` | Repositorio que clona el user data |
| `alarmEmail` | Correo suscrito al topic de alertas |

Sin `adminCidr` el grupo de seguridad no abre el puerto 22 y el acceso administrativo va por Session Manager. Sin `frontendDomainName` ni `certificateArn`, CloudFront queda con su dominio propio.

## Secretos y despliegue

El despliegue de sagas exige los parámetros `DatabaseSecretArn` y `WalletSecretArn`. Solo sus ARN llegan al entorno Lambda; cada rol puede leer exclusivamente los secretos que requiere. No se guardan valores en Git, contexto o CloudFormation.

El rol de Step Functions solo invoca sus Lambdas. El rol de Connector limita `StartExecution` a ambos alias. `SendTaskSuccess/Failure` y las APIs de entrega de CloudWatch Logs usan `Resource: *` porque AWS no admite delimitarlas por ARN; no hay permisos administrativos.

La estrategia sigue siendo tests, `cdk synth`, `cdk diff`, revisión y despliegue inicial en `dev`. Producción requiere aprobación y rollback. Esta rama no ejecuta `cdk deploy`.

El user data instala Docker y, si `appRepositoryUrl` está definido, clona el repositorio y levanta `docker-compose.prod.yml`. Sin esa clave el host queda sin aplicación y lo informa en `/var/log/energyshark-bootstrap.log`.

## Validación local

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r lambda_src/saga_task/requirements.txt
.venv/bin/python -m unittest discover -s tests -v
CDK_OUTDIR=cdk.out .venv/bin/python app.py
```

Validación del contrato con CloudFormation, sin desplegar:

```bash
aws cloudformation validate-template \
  --template-body file://cdk.out/EnergySharkDev-ExistingInfrastructure.template.json
aws cloudformation validate-template \
  --template-body file://cdk.out/EnergySharkDev-Monitoring.template.json
```

El template de sagas supera el límite de 51 200 bytes de `validate-template`; se valida con sus tests y un change set en una cuenta aislada (E3-078).

Auditoría de permisos y secretos (E3-077), sin desplegar:

```bash
python3 ../../tools/iac_secrets_scan.py
python3 ../../tools/iac_policy_audit.py \
  cdk.out/EnergySharkDev-ExistingInfrastructure.template.json --profile <perfil>
python3 ../../tools/iac_policy_audit.py \
  cdk.out/EnergySharkDev-Sagas.template.json --profile <perfil>
```

## Permisos

El rol de la instancia conserva `AmazonSSMManagedInstanceCore`, la política mínima de AWS para Session Manager, y una política en línea que acota métricas al namespace `EnergyShark/Host` y logs al grupo `/energyshark/host`. No hay acciones comodín ni políticas de administrador en ningún stack. Los recursos comodín que quedan están justificados por API en `docs/E3-077-permisos-minimos-secretos-iac.md`.
