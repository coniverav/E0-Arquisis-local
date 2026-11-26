# E3-055 · Despliegue verificable de las sagas

El stack `EnergySharkDev-Sagas` se puede revisar y desplegar por separado en una cuenta de pruebas. La base revisada es `develop` en `0626de1`. Contiene E3-019/020/021 en `3d5c19e` y E3-022 en `24f928c`. El inventario E3-010 describe el estado observado en esa tarjeta. El estado actual de la cuenta requiere una comparación con CloudFormation.

| Recurso | Configuración de `dev` |
| --- | --- |
| SELLER y BUYER | Standard, 1.800 s cada una. ASL versionada, cifrado explícito `AWS_OWNED_KEY`, versión y alias `live` por máquina. |
| 13 Lambdas | Python 3.12 en una imagen común `linux/amd64`, base fijada por digest, 256 MiB y 25 s. CDK toma el código y las dependencias de `lambda_src/saga_task`. |
| 15 grupos de logs | Uno por función y por máquina, con retención de 7 días en dev, 14 en staging y 30 en prod. CloudWatch cifra los logs en reposo. Las máquinas registran nivel `ALL` con `IncludeExecutionData=false`. |
| Roles y secretos | Cada máquina espera la creación de su política de invocación y entrega de logs. Cada función usa un rol propio y el cifrado de entorno de Lambda. Las variables identifican tarea, ciudad, ambiente y, según la tarea, ARN completos de Secrets Manager. |
| Invocador E3-056 | Rol EC2 y perfil de instancia declarados. `StartExecution` se limita a los alias y los callbacks usan el task token. La asociación efectiva al host y la activación de los relays corresponden a E3-056. |

CloudFormation asigna los nombres físicos de funciones, máquinas, grupos, roles y perfil. El identificador de stack distingue ambientes y el alias `live` pertenece a su máquina. Los 19 outputs identifican recursos propios mediante ARN. Para E3-056, `SellerSagaAliasArn` corresponde a `E3_SELLER_STATE_MACHINE_ARN` y `BuyerSagaAliasArn` a `E3_BUYER_STATE_MACHINE_ARN`. El perfil se obtiene de `ConnectorSagaInstanceProfileArn`. Un ejemplo de evidencia redactada es `arn:aws:states:<región>:<cuenta>:stateMachine:<máquina>:live`. Para compartir el inventario basta con listar las claves de los outputs.

## Pruebas y síntesis local

Desde la raíz del backend, usar Bash, Python 3.12, Node 22 y CDK CLI 2.1149.0. Ejecutar la síntesis en una sesión local sin credenciales AWS. La app también sintetiza el stack E2, cuyo mapa de AMI por defecto usa `us-east-2`.

```bash
cd infra/cdk
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt -r lambda_src/saga_task/requirements.txt
npm install --prefix .venv/cli --no-audit --no-fund aws-cdk@2.1149.0
export PATH="$PWD/.venv/cli/node_modules/.bin:$PATH"
export AWS_REGION=us-east-2 AWS_DEFAULT_REGION=us-east-2 AWS_EC2_METADATA_DISABLED=true
export PYTHONDONTWRITEBYTECODE=1
timeout 420 python -m unittest discover -s tests -v
timeout 180 cdk synth EnergySharkDev-Sagas -c environment=dev --no-lookups --quiet -o cdk.out
python -c 'import json; print(*json.load(open("cdk.out/EnergySharkDev-Sagas.template.json"))["Outputs"], sep="\n")'
```

Para reproducir la comparación local contra la base exacta, mantener el mismo CDK, región y directorio. El archivo de Git se extrae en el entorno de pruebas.

```bash
base=$(mktemp -d "$PWD/.venv/e3055-base.XXXXXX")
git -C ../.. archive 0626de1:infra/cdk | tar -x -C "$base"
timeout 180 cdk synth EnergySharkDev-Sagas -c environment=dev --no-lookups --quiet \
  --app "python \"$base/app.py\"" -o "$base/out"
timeout 90 cdk diff EnergySharkDev-Sagas --app cdk.out --exclusively \
  --method=template --template "$base/out/EnergySharkDev-Sagas.template.json"
for stack in ExistingInfrastructure Monitoring; do
  cmp "$base/out/EnergySharkDev-$stack.template.json" "cdk.out/EnergySharkDev-$stack.template.json" || exit 1
done
```

La comparación marca reemplazos dentro de Sagas al retirar los nombres físicos fijos. Si la versión anterior del stack existe en la cuenta, detenerse antes de desplegar y revisar los ARN usados por los relays y las ejecuciones en curso. Los grupos antiguos tienen política de retención.

## Revisión del ambiente de prueba

Seleccionar el perfil de la cuenta de pruebas y verificar la identidad antes de usar el assembly revisado. El bootstrap estándar aporta S3, ECR y los roles de publicación. El manifiesto declara versión mínima 6 para el stack y 8 para el rol de lookup. La preparación del bootstrap, si falta, requiere autorización separada.

```bash
export AWS_PROFILE="${PERFIL_AWS_DEV:?indicar perfil de pruebas}"
test "$(aws sts get-caller-identity --query Account --output text)" = "${CUENTA_AWS_DEV:?indicar cuenta de pruebas}" || exit 1
aws ssm get-parameter --name /cdk-bootstrap/hnb659fds/version --query Parameter.Value --output text
timeout 120 cdk diff EnergySharkDev-Sagas --app cdk.out --exclusively --method=template
```

Este diff de lectura compara con la plantilla de la cuenta. Revisar también que el destino permita a las Lambdas llegar a PostgreSQL, Secrets Manager, Wallet y JWKS. La plantilla usa la red por defecto del servicio Lambda. Si PostgreSQL requiere acceso a una VPC privada, hace falta definir las subredes, el grupo de seguridad y la salida HTTPS aprobados antes de habilitar el flujo. Para secretos cifrados con una clave KMS del cliente, revisar además el permiso `kms:Decrypt` sobre esa clave.

## Despliegue sujeto a aprobación explícita

Con Docker disponible, el mismo assembly revisado y los dos ARN de la cuenta de pruebas en variables del operador, el siguiente comando publica la imagen del asset y crea o actualiza exclusivamente el stack de sagas.

```bash
cdk deploy EnergySharkDev-Sagas --app cdk.out --exclusively --require-approval any-change \
  --parameters "EnergySharkDev-Sagas:DatabaseSecretArn=${DATABASE_SECRET_ARN:?indicar ARN de pruebas}" \
  --parameters "EnergySharkDev-Sagas:WalletSecretArn=${WALLET_SECRET_ARN:?indicar ARN de pruebas}"
```

Tras la aprobación y el despliegue, recopilar el estado de CloudFormation y las claves de output. Estas consultas sirven como evidencia compartible sin mostrar los ARN de la cuenta.

```bash
aws cloudformation describe-stacks --stack-name EnergySharkDev-Sagas \
  --query 'Stacks[0].{Estado:StackStatus,Outputs:Outputs[].OutputKey}'
aws cloudformation list-stack-resources --stack-name EnergySharkDev-Sagas \
  --query 'StackResourceSummaries[].{Tipo:ResourceType,Estado:ResourceStatus}'
```

## Evidencia local del 26 de noviembre de 2026

| Verificación | Resultado observado |
| --- | --- |
| Suite de `infra/cdk/tests` | `Ran 149 tests in 94.207s` y `OK`, con timeout de 420 s. Incluye ASL, IAM, callbacks y flujos Lambda con dobles de PostgreSQL y Wallet. |
| Dos síntesis independientes de dev | Plantilla de Sagas y manifiesto de assets idénticos byte por byte. SHA-256 de la plantilla: `b0ec6a258ec9840844c09768efd0ee52289acf3fe164f8b81aed1d274269107d`. |
| Validación de CloudFormation durante la síntesis | Resultado local `success`. La única advertencia, `W9010`, corresponde a la AMI del stack E2. |
| Diff local contra `0626de1` | `Number of stacks with differences: 1`. El diff estima 34 reemplazos potenciales en Sagas (13 Lambdas, 15 grupos de logs, 2 máquinas, 2 versiones, rol y perfil del Connector). Las plantillas de E2 y Monitoring son idénticas a la base. |
| Imagen de prueba | Build Docker local para `linux/amd64` correcto. El handler empaquetado ejecutó `check-buyer-deadline` en un contenedor de solo lectura sin red y devolvió `{"eligible":true,"reason":"ENOUGH_TIME"}`. |
| Outputs y secretos | 19 ARN referidos a recursos de Sagas. Los tests comprueban tipo y destino de cada output. El escáner local informó cero hallazgos en `infra/cdk` y en esta guía. |

El diff contra una cuenta de pruebas, la conectividad real con PostgreSQL y Wallet, el despliegue y el historial de ejecución quedan pendientes.
