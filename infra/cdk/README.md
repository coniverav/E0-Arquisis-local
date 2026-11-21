# Base IaC de EnergyShark

Se eligió AWS CDK v2 con Python para mantener IaC revisable y cercana al backend. Esta base solo define límites de stacks; no crea ni despliega recursos.

## Estructura

- `app.py`: aplicación CDK y configuración común.
- `energyshark_infra/config.py`: ambientes y nombres de stacks.
- `stacks/existing_infrastructure.py`: recursos E2 existentes.
- `stacks/sagas.py`: futuras Step Functions y Lambdas.
- `stacks/monitoring.py`: futuras métricas, alarmas y paneles.
- `tests/`: valida límites y ausencia de recursos en la base.

## Estado y ambientes

Los recursos E2 no están importados ni administrados por estos stacks. CloudFormation solo recibirá recursos después de una revisión e importación explícita. No existe un despliegue CDK de esta base.

Los ambientes soportados son `dev`, `staging` y `prod`, seleccionados con contexto CDK `environment`. Cuenta y región se resuelven desde `CDK_DEFAULT_ACCOUNT` y `CDK_DEFAULT_REGION`; no se guardan identificadores en Git. Producción activa protección contra eliminación.

## Secretos y despliegue

Las credenciales y secretos permanecen fuera de Git. Los recursos futuros deben referenciarlos desde Secrets Manager o SSM Parameter Store, sin incluir valores en código, contexto ni CloudFormation.

La estrategia es ejecutar tests, `cdk synth` y `cdk diff`, revisar la plantilla, importar recursos existentes por separado y desplegar primero en `dev`. Sagas y monitoreo se incorporarán en cambios pequeños; producción requerirá aprobación manual y plan de rollback.

## Validación local

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python app.py
```
