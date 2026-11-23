# E3-077 · Permisos mínimos y secretos fuera de IaC

Auditoría y ajuste del IAM que declaran los stacks CDK, más una verificación reproducible de que el repositorio no contiene credenciales. Se apoya en E3-076: el stack E2 ya se declara completo, así que ahora se acota lo que concede.

## Cambio aplicado

El rol de la instancia tenía dos políticas administradas de AWS: `AmazonSSMManagedInstanceCore` y `CloudWatchAgentServerPolicy`. La segunda concede escritura de logs y métricas sobre todo el recurso.

Se reemplazó por tres sentencias en línea:

| Sid | Acción | Recurso | Condición |
| :--- | :--- | :--- | :--- |
| `PublishHostMetrics` | `cloudwatch:PutMetricData` | `*` | `cloudwatch:namespace` = `EnergyShark/Host` |
| `WriteAgentLogs` | `logs:CreateLogStream`, `logs:PutLogEvents`, `logs:DescribeLogStreams` | ARN del log group `/energyshark/host` y `:*` | — |
| `DescribeHostForDimensions` | `ec2:DescribeInstances`, `ec2:DescribeTags`, `ec2:DescribeVolumes` | `*` | — |

`AmazonSSMManagedInstanceCore` se conserva: AWS la documenta como el conjunto mínimo para Session Manager y reemplazarla a mano solo agrega riesgo de romper el acceso administrativo.

Se agregó el log group `/energyshark/host`, que es el destino real del agente CloudWatch y el recurso al que se acotan las escrituras.

## Inventario de `Resource: "*"`

Todas las sentencias con recurso comodín, con su justificación:

| Stack | Acciones | Motivo |
| :--- | :--- | :--- |
| E2 | `cloudwatch:PutMetricData` | AWS no admite ARN; se acota por la condición de namespace |
| E2 | `ec2:DescribeInstances`, `ec2:DescribeTags`, `ec2:DescribeVolumes` | Las API `Describe*` no admiten ARN |
| Sagas | `states:SendTaskSuccess`, `states:SendTaskFailure` | Solo aceptan `*`; la capacidad real es el task token |
| Sagas | `logs:CreateLogDelivery`, `GetLogDelivery`, `UpdateLogDelivery`, `DeleteLogDelivery`, `ListLogDeliveries`, `PutResourcePolicy`, `DescribeResourcePolicies`, `DescribeLogGroups` | Entrega de logs de Step Functions no admite ARN |

No hay `Action: "*"`, ni `AdministratorAccess`, ni `PowerUserAccess`, ni `iam:*` en ningún stack. El test `test_no_role_grants_wildcard_actions` falla si aparece uno.

## Secretos

- Los tres parámetros de sagas (`DatabaseSecretArn`, `WalletSecretArn`, `BrokerSecretArn`) ya declaran `NoEcho: true`.
- Las Lambdas reciben solo ARN: cada variable terminada en `_SECRET_ARN` referencia un parámetro `NoEcho`, nunca un valor literal. El test `test_lambda_receives_secret_arns_not_values` lo verifica.
- Cada rol de tarea solo puede leer los secretos que necesita, por ARN.
- El repositorio no contiene credenciales: `.env.example` es el único archivo con nombre de entorno y no tiene valores reales.

Escaneo del árbol completo:

```
$ python3 tools/iac_secrets_scan.py
Sin secretos detectados en 1 ruta(s).
```

El escáner detecta claves de AWS (`AKIA`/`ASIA`), bloques PEM, tokens de GitHub, secretos de Auth0, archivos `.env` y llaves `*.pem`/`*.key`/`*.p12`. Salta `.git`, `cdk.out`, `.venv`, `node_modules` y binarios.

## Auditoría con IAM Access Analyzer

Se extrajeron todas las políticas de permisos de los templates sintetizados y se validaron contra el servicio real:

```
$ python3 tools/iac_policy_audit.py infra/cdk/cdk.out/EnergySharkDev-ExistingInfrastructure.template.json --profile arquisis
OK CustomVpcRestrictDefaultSGCustomResourceProviderRole.../inline[0] (0 hallazgo(s))
OK ApplicationInstanceRoleDefaultPolicy... (0 hallazgo(s))

Políticas: 2 · errores: 0

$ python3 tools/iac_policy_audit.py infra/cdk/cdk.out/EnergySharkDev-Sagas.template.json --profile arquisis
...
Políticas: 16 · errores: 0
```

Total: **18 políticas de permisos, 0 errores, 0 advertencias de seguridad**. El stack `Monitoring` no declara políticas.

El auditor sustituye las funciones intrínsecas de CloudFormation por ARN de ejemplo del servicio correcto antes de validar, porque `validate-policy` no acepta `Fn::GetAtt` ni `Ref`. Las políticas de confianza quedan fuera: solo indican quién asume el rol, no qué permisos concede.

## Cómo reproducirlo

```bash
cd infra/cdk
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
CDK_OUTDIR=cdk.out .venv/bin/python app.py

cd ../..
python3 tools/iac_secrets_scan.py
python3 tools/iac_policy_audit.py infra/cdk/cdk.out/EnergySharkDev-ExistingInfrastructure.template.json --profile arquisis
python3 tools/iac_policy_audit.py infra/cdk/cdk.out/EnergySharkDev-Sagas.template.json --profile arquisis
```

## Tests

| Archivo | Cobertura |
| :--- | :--- |
| `infra/cdk/tests/test_e3077_iam_least_privilege.py` | 9 tests: sin acciones comodín, recursos comodín justificados, una sola política administrada, condición de namespace, logs acotados, `NoEcho`, ARN en variables, sin credenciales en templates |
| `tools/tests/test_iac_secrets_scan.py` | 9 tests: detección por tipo, `.env.example` ignorado, exclusiones de build, repositorio limpio |
| `tools/tests/test_iac_policy_audit.py` | 9 tests: resolución de `GetAtt`, `Ref`, `Sub`, `Join` y extracción de políticas |

```
$ python -m unittest discover -s tools/tests
Ran 23 tests
OK

$ cd infra/cdk && python -m unittest discover -s tests
Ran 122 tests
FAILED (failures=3, errors=2)
```

Los cinco fallos de la suite de infraestructura son previos a este cambio y existen igual en `develop` (import de `test_e3045_buyer_validation`, log sensible en `test_serverless_iac`, token de comprador en `test_e3035_saga_paths`). No los toqué.

## Límites declarados

- **No se modificó el IAM real de la cuenta.** El alcance es el IAM que declara el IaC; la cuenta sigue con el usuario `grupo10` sin MFA, declarado como pendiente en E3-071.
- Los roles de recursos personalizados que genera CDK (`Custom::S3AutoDeleteObjects`, `Custom::VpcRestrictDefaultSG`) usan `AWSLambdaBasicExecutionRole`. Es la política mínima de AWS para logs de Lambda; se deja como está y se documenta.
- La validación de políticas es estática: confirma que las sentencias son coherentes y acotadas, no mide acceso efectivo. El ensayo en cuenta aislada corresponde a E3-078.

## Archivos

- `infra/cdk/energyshark_infra/stacks/existing_infrastructure.py`: rol acotado y log group del host.
- `infra/cdk/tests/test_e3077_iam_least_privilege.py`: 9 tests nuevos.
- `infra/cdk/tests/test_e3076_reproducible_stack.py`: expectativas actualizadas (rol y log groups).
- `tools/iac_secrets_scan.py`: escáner de secretos.
- `tools/iac_policy_audit.py`: auditor con IAM Access Analyzer.
- `tools/tests/test_iac_secrets_scan.py`, `tools/tests/test_iac_policy_audit.py`: 18 tests.
