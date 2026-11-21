# IaC de EnergyShark

Se usa AWS CDK v2 con Python. La infraestructura E2 y monitoreo conservan límites vacíos hasta su migración; el stack de sagas ya declara la parte serverless E3, pero todavía no se ha desplegado.

## Estructura

- `app.py`: aplicación CDK y configuración común.
- `energyshark_infra/config.py`: ambientes y nombres de stacks.
- `stacks/existing_infrastructure.py`: recursos E2 existentes.
- `stacks/sagas.py`: Step Functions Standard, Lambdas, logs e IAM.
- `asl/`: definiciones seller/buyer versionadas.
- `lambda_src/saga_task/`: imagen común reproducible para tareas de saga.
- `stacks/monitoring.py`: futuras métricas, alarmas y paneles.
- `tests/`: valida límites y ausencia de recursos en la base.

## Estado y ambientes

Los recursos E2 no están importados ni administrados por estos stacks. El stack de sagas crea dos máquinas Standard, versiones con alias `live`, 13 Lambdas y roles mínimos. Las tareas Wallet y de callbacks usan el adaptador durable; las demás fallan de forma segura hasta su implementación.

`dev`, `staging` y `prod` parametrizan nombres, timeout total, memoria, timeout Lambda y retención de logs. Cuenta y región provienen de `CDK_DEFAULT_ACCOUNT` y `CDK_DEFAULT_REGION`; producción activa protección contra eliminación.

## Secretos y despliegue

El despliegue exige los parámetros `DatabaseSecretArn`, `WalletSecretArn` y `BrokerSecretArn`. Solo sus ARN llegan al entorno Lambda; cada rol puede leer exclusivamente los secretos que requiere. No se guardan valores en Git, contexto o CloudFormation.

El rol de Step Functions solo invoca sus Lambdas. El rol de Connector limita `StartExecution` a ambos alias. `SendTaskSuccess/Failure` y las APIs de entrega de CloudWatch Logs usan `Resource: *` porque AWS no admite delimitarlas por ARN; no hay permisos administrativos.

La estrategia sigue siendo tests, `cdk synth`, `cdk diff`, revisión y despliegue inicial en `dev`. Producción requiere aprobación y rollback. Esta rama no ejecuta `cdk deploy`.

## Validación local

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python app.py
```
