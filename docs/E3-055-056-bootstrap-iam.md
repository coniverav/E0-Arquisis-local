# IAM para bootstrap y despliegue de sagas

Esta guía prepara el bootstrap CDK y el despliegue exclusivo de
`EnergySharkProd-Sagas` en la cuenta `521294961974`, región `us-east-2`. No
autoriza desplegar `ExistingInfrastructure`, `Monitoring` ni modificar E2.

## Documentos

Los documentos versionados en `infra/cdk/iam/` son políticas administradas por
el cliente, no roles ni credenciales:

| Política | Uso |
| --- | --- |
| `EnergySharkCdkBootstrapOperator-us-east-2` | Permiso temporal de `grupo10` para crear solamente `CDKToolkit`. |
| `EnergySharkSagasCfnExecution-us-east-2` | Permisos de CloudFormation para los recursos de `EnergySharkProd-Sagas`. |
| `EnergySharkCdkBootstrapBoundary-us-east-2` | Límite máximo de todos los roles del bootstrap. |
| `EnergySharkCdkDeployCaller-us-east-2` | Permiso permanente de `grupo10` para asumir los tres roles de publicación/despliegue. |

Ninguna política contiene `AdministratorAccess`. El rol de lookup queda fuera:
todo comando operativo debe usar `--no-lookups`.

## Preparación en la consola

Esta operación requiere temporalmente un administrador IAM distinto del
despliegue normal.

1. En **IAM > Policies > Create policy > JSON**, crear primero la boundary y
   luego las políticas de ejecución, bootstrap y caller usando exactamente los
   nombres y archivos anteriores.
2. Adjuntar temporalmente `EnergySharkCdkBootstrapOperator-us-east-2` al
   usuario `grupo10`. No adjuntar `AdministratorAccess`.
3. No crear manualmente los roles `cdk-hnb659fds-*`: los crea `CDKToolkit`.
4. Finalizado el bootstrap, retirar la política temporal y adjuntar solamente
   `EnergySharkCdkDeployCaller-us-east-2` a `grupo10`.

Antes de aplicar una política, IAM Access Analyzer debe validarla como política
de identidad:

```bash
cd "$HOME/Proyecto Arquisis/E0-Arquisis-local"
for policy in infra/cdk/iam/*.json; do
  aws accessanalyzer validate-policy \
    --region us-east-2 \
    --policy-type IDENTITY_POLICY \
    --policy-document "file://$policy"
done
```

La salida debe carecer de errores. Esta validación consulta AWS, por lo que no
forma parte de las pruebas offline del repositorio.

## Bootstrap desde WSL

Verificar primero la sesión MFA y detenerse si la cuenta no coincide:

```bash
aws sts get-caller-identity
aws configure get region
```

Luego ejecutar una sola vez:

```bash
cd "$HOME/Proyecto Arquisis/E0-Arquisis-local/infra/cdk"
export AWS_ACCOUNT_ID=521294961974
export AWS_REGION=us-east-2
export AWS_DEFAULT_REGION=us-east-2
export CDK_DEFAULT_ACCOUNT="$AWS_ACCOUNT_ID"
export CDK_DEFAULT_REGION="$AWS_REGION"

.venv/cli/node_modules/.bin/cdk bootstrap \
  "aws://${AWS_ACCOUNT_ID}/${AWS_REGION}" \
  --cloudformation-execution-policies \
  "arn:aws:iam::${AWS_ACCOUNT_ID}:policy/EnergySharkSagasCfnExecution-us-east-2" \
  --custom-permissions-boundary EnergySharkCdkBootstrapBoundary-us-east-2 \
  --apply-permissions-boundary-to-all-roles \
  --no-bootstrap-customer-key \
  --termination-protection
```

No usar `--trust`: el bootstrap queda en la misma cuenta. La clave administrada
por AWS evita crear una KMS key; cambiar esa opción requiere ampliar S3/KMS.

## Verificación posterior al bootstrap

Comprobar `CDKToolkit`, la versión y cada rol antes de retirar el permiso
temporal:

```bash
aws cloudformation describe-stacks \
  --region us-east-2 --stack-name CDKToolkit
aws ssm get-parameter --region us-east-2 \
  --name /cdk-bootstrap/hnb659fds/version

for role in deploy file-publishing image-publishing lookup cfn-exec; do
  aws iam get-role \
    --role-name "cdk-hnb659fds-${role}-role-521294961974-us-east-2" \
    --query 'Role.[RoleName,PermissionsBoundary.PermissionsBoundaryArn]'
done

aws iam list-attached-role-policies \
  --role-name cdk-hnb659fds-cfn-exec-role-521294961974-us-east-2
```

Los cinco roles deben mostrar la boundary y el rol `cfn-exec` debe tener
`EnergySharkSagasCfnExecution-us-east-2`, nunca `AdministratorAccess`.
Esa política permite `ssm:GetParameters` únicamente sobre
`/cdk-bootstrap/hnb659fds/version`, que CloudFormation consulta al preparar el
change set para validar la versión del bootstrap.

## Simulación de permisos

IAM Policy Simulator no sustituye un change set, pero permite verificar casos
positivos y negativos. Ejemplo para la política de ejecución más su boundary:

```bash
cd "$HOME/Proyecto Arquisis/E0-Arquisis-local"
aws iam simulate-custom-policy \
  --policy-input-list file://infra/cdk/iam/EnergySharkSagasCfnExecution-us-east-2.json \
  --permissions-boundary-policy-input-list file://infra/cdk/iam/EnergySharkCdkBootstrapBoundary-us-east-2.json \
  --action-names lambda:CreateFunction \
  --resource-arns arn:aws:lambda:us-east-2:521294961974:function:EnergySharkProd-Sagas-Test \
  --context-entries \
    ContextKeyName=aws:RequestTag/Project,ContextKeyValues=EnergyShark,ContextKeyType=string \
    ContextKeyName=aws:RequestTag/Environment,ContextKeyValues=prod,ContextKeyType=string \
    ContextKeyName=aws:RequestTag/ManagedBy,ContextKeyValues=AWS-CDK,ContextKeyType=string
```

Casos mínimos para registrar antes del despliegue:

| Caso | Resultado esperado |
| --- | --- |
| `sts:AssumeRole` a deploy/file/image publisher con la política caller | `allowed` |
| `sts:AssumeRole` al lookup role o a un rol ajeno | `implicitDeny` |
| Crear Lambda `EnergySharkProd-Sagas-*` con las tres etiquetas | `allowed` |
| Crear Lambda con otro prefijo o sin etiquetas | `implicitDeny` |
| `iam:PassRole` de un rol del stack a Lambda/Step Functions/EC2 | `allowed` |
| Mismo `PassRole` a ECS o sobre un rol ajeno | `implicitDeny` |
| Crear/actualizar `EnergySharkProd-Sagas` | permitido por la boundary |
| Modificar `ExistingInfrastructure` o `Monitoring` | `implicitDeny` |
| `cloudformation:DeleteStack` | `implicitDeny` |
| Crear un security group en `us-east-2` con las etiquetas requeridas | `allowed` |
| Crear el security group en otra región o sin etiquetas | `implicitDeny` |

Para `iam:PassRole` se debe incluir el contexto
`iam:PassedToService=lambda.amazonaws.com` (o el servicio correspondiente). El
simulador puede requerir contexto adicional y no modela todos los controles del
servicio; revisar siempre el change set real.

## Synth, diff y despliegue futuro

La validación local no consulta AWS:

```bash
cd "$HOME/Proyecto Arquisis/E0-Arquisis-local/infra/cdk"
AWS_EC2_METADATA_DISABLED=true JSII_RUNTIME_PACKAGE_CACHE_ROOT=/tmp/jsii-cache \
  .venv/cli/node_modules/.bin/cdk synth EnergySharkProd-Sagas \
  -c environment=prod -c deploymentScope=sagas \
  --exclusively --no-lookups --quiet \
  --app '.venv/bin/python app.py' -o /tmp/energyshark-sagas
```

Tras el bootstrap, ejecutar `cdk diff` con los mismos contextos y revisar que
solo aparezca `EnergySharkProd-Sagas`. El despliegue autorizado será:

```bash
.venv/cli/node_modules/.bin/cdk deploy EnergySharkProd-Sagas \
  -c environment=prod -c deploymentScope=sagas \
  --exclusively --no-lookups --require-approval broadening \
  --parameters ExistingVpcId=<vpc-id> \
  --parameters ExistingSubnetId=<subnet-id> \
  --parameters ApplicationPrivateIp=<ip-privada-ec2>
```

No usar IP pública ni ejecutar `cdk deploy --all`. Mantener seller/buyer
desactivados y seguir la instalación/rollback de `E3-055-step-functions-iac.md`.

## Acciones que requieren `Resource: "*"`

- CloudFormation: validación, listados y límites de cuenta no admiten ARN.
- `sts:GetCallerIdentity` y `ecr:GetAuthorizationToken` no admiten ARN.
- EC2 `Describe*` no admite ARN; creación y reglas de security groups requieren
  alcance amplio en parte de su ciclo, acotado por región y etiquetas cuando el
  API las expone.
- `logs:DescribeLogGroups` y `secretsmanager:GetRandomPassword` no ofrecen un
  recurso concreto.

## Riesgos y revisión manual

- `iam:PutRolePolicy` no permite limitar el contenido de una política inline.
  El prefijo de rol, `iam:PassRole`, la boundary de bootstrap y la revisión del
  change set reducen el riesgo, pero no reemplazan una boundary separada para
  roles de aplicación. Incorporarla exigiría cambiar el stack CDK.
- Los nombres físicos los asigna CloudFormation. Antes del primer deploy hay
  que confirmar en el template/change set que conservan el prefijo
  `EnergySharkProd-Sagas-` esperado por la política.
- La boundary restringe deliberadamente el lookup role; por eso `--no-lookups`
  es obligatorio.
- Policy Simulator y Access Analyzer son controles previos; el primer deploy
  debe conservar aprobación manual y monitoreo de `AccessDenied`.
