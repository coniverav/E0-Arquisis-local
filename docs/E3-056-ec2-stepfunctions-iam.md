# E3-056 · IAM de EC2 y Step Functions

La base es `develop` en `8f51e8d`. E3-021 está integrada mediante `3d5c19e`, E3-055 mediante PR #136 y E3-030 mediante PR #112. El stack de sagas declara el rol EC2 y su instance profile. Su trust policy permite `sts:AssumeRole` al servicio `ec2.amazonaws.com`.

| Llamada | Permiso del rol EC2 |
| --- | --- |
| Inicio seller/buyer | `states:StartExecution` sobre los dos alias administrados por el stack |
| Recuperación de `ExecutionAlreadyExists` | `states:DescribeExecution` sobre `execution:<nombre de máquina>:*` de ambas máquinas, dentro de la cuenta y región del stack |
| Callback comprador | `states:SendTaskSuccess` con `Resource: "*"` |
| Failure y heartbeat | Ausentes, porque los flujos actuales no invocan esas API |

AWS exige recurso comodín para `SendTaskSuccess`. El token es una capacidad opaca de la espera y debe proceder del claim durable de Master. La Lambda seller-proposal también necesita `SendTaskSuccess` para completar la propuesta conocida. El resto de las Lambdas carece de este permiso. [Referencia de autorización de AWS](https://docs.aws.amazon.com/service-authorization/latest/reference/list_stepfunctions.html).

El callback comprador se reclama después del commit Wallet. Master comprueba rol BUYER, estado WAITING, correlación entre saga, token y swap, vencimiento y resultado Wallet terminal. Connector valida identificadores, claim y resultado antes del envío. Una denegación AWS conserva el circuito de reintento y nunca confirma el callback localmente.

El inicio exige el ARN configurado para el rol del input, nombre igual a idpk, correlación con sagaExecutionId cuando esté presente y una respuesta con el ARN esperado. Ante un nombre existente se comprueban máquina, nombre e input persistido. La lista permitida proviene de `E3_SELLER_STATE_MACHINE_ARN` y `E3_BUYER_STATE_MACHINE_ARN`, suministrados a Connector por Compose. Configurarlos con los outputs de alias de E3-055 antes de habilitar el relay.

Los endpoints de claims y resultados deben compartir esquema, host y puerto con `MASTER_URL`. Los clientes HTTP del relay verifican TLS y desactivan redirecciones y proxies heredados. Para HTTP dentro de Compose, la autenticidad de Master depende del aislamiento de esa red. Nginx bloquea `/internal/` y el API Gateway declara únicamente rutas públicas. Verificar ese aislamiento y usar HTTPS autenticado si el relay cruza una frontera de confianza.

## Credenciales y comprobaciones de lectura

Boto3 usa su cadena de proveedores y mantiene el objeto de credenciales renovables. En EC2, retirar claves estáticas de variables, archivos y contenedores para que el SDK utilice el rol del instance profile mediante IMDSv2. Una configuración STS administrada por el SDK también conserva la renovación. El cliente fija timeouts y reintentos acotados, y no copia claves ni tokens de sesión.

Con AWS CLI disponible, ejecutar desde el host o contenedor de pruebas que usa el relay. Registrar únicamente identidad y resultados redactados.

```bash
timeout 30 aws sts get-caller-identity --query '{Account:Account,Arn:Arn}'
timeout 30 aws ec2 describe-iam-instance-profile-associations \
  --filters "Name=instance-id,Values=${INSTANCE_ID:?indicar instancia de prueba}"
```

Obtener `ROLE_ARN`, `SELLER_ALIAS_ARN` y `BUYER_ALIAS_ARN` de los outputs del stack. Construir `SELLER_EXECUTION_ARN` con el nombre físico real de la máquina y una ejecución de prueba. Usar un ARN ajeno como control de denegación. El operador del simulador requiere `iam:SimulatePrincipalPolicy`; ese permiso pertenece al operador, fuera del rol de ejecución.

```bash
timeout 30 aws iam simulate-principal-policy \
  --policy-source-arn "${ROLE_ARN:?}" --action-names states:StartExecution \
  --resource-arns "${SELLER_ALIAS_ARN:?}" "${BUYER_ALIAS_ARN:?}" "${OTHER_ALIAS_ARN:?}" \
  --query 'EvaluationResults[].{Action:EvalActionName,Decision:EvalDecision}'
timeout 30 aws iam simulate-principal-policy \
  --policy-source-arn "$ROLE_ARN" --action-names states:DescribeExecution \
  --resource-arns "${SELLER_EXECUTION_ARN:?}" "${OTHER_EXECUTION_ARN:?}" \
  --query 'EvaluationResults[].{Action:EvalActionName,Decision:EvalDecision}'
timeout 30 aws iam simulate-principal-policy \
  --policy-source-arn "$ROLE_ARN" \
  --action-names states:SendTaskSuccess states:SendTaskFailure states:SendTaskHeartbeat \
  --resource-arns '*' \
  --query 'EvaluationResults[].{Action:EvalActionName,Decision:EvalDecision}'
```

Se espera allow para los alias propios y ejecuciones propias, implicitDeny para los recursos ajenos y para Failure/Heartbeat. El simulador evalúa permisos IAM. La validez y pertenencia del task token requieren una ejecución de prueba autorizada.

## Asociación al host y continuidad

La asociación actual del host productivo queda pendiente de inventario. Los ARN físicos se obtienen mediante consultas a la cuenta, evitando deducirlos de nombres anteriores. El stack E2 define un rol con SSM y observabilidad, mientras que Sagas define un perfil independiente. El perfil de Sagas por sí solo carece de esos permisos operativos.

Para una actualización aprobada, identificar primero el rol y perfil realmente asociados y guardar la asociación para rollback. Si el host conserva su rol operativo, incorporar la política mínima de sagas a ese rol mediante su IaC propietaria y revisar el diff. Si se elige cambiar de perfil, preparar previamente un rol que conserve los permisos operativos existentes y añada únicamente los permisos de esta tarjeta. Validar STS, SSM, observabilidad y Policy Simulator en pruebas antes del cambio.

EC2 admite reemplazar una asociación de instance profile existente. Aplicar el reemplazo bajo aprobación, esperar la propagación y verificar la identidad desde el contenedor y una ejecución de prueba. La rotación puede producir denegaciones transitorias; los leases, los nombres estables y la corroboración de replays conservan la intención durable. Ante fallo, restaurar la asociación guardada y comprobar la recuperación del outbox. Las ejecuciones en curso mantienen sus identificadores.

## Validación reproducible

Desde la raíz del repositorio, utilizar el entorno de CDK de E3-055 y Node 22.

```bash
infra/cdk/.venv/bin/pip install -r connector/requirements.txt pytest
timeout 180 env PYTHONPATH=connector AWS_EC2_METADATA_DISABLED=true \
  infra/cdk/.venv/bin/python -m pytest connector/tests -q
cd infra/cdk
source .venv/bin/activate
export PATH="$PWD/.venv/cli/node_modules/.bin:$PATH"
export AWS_REGION=us-east-2 AWS_DEFAULT_REGION=us-east-2
timeout 420 python -m unittest discover -s tests -v
timeout 180 cdk synth EnergySharkDev-Sagas --no-lookups --quiet -o cdk.out
```

Para el diff local, sintetizar la base `8f51e8d` mediante el procedimiento documentado en E3-055 y comparar su template con el nuevo assembly.

```bash
timeout 90 cdk diff EnergySharkDev-Sagas --app cdk.out --exclusively \
  --method=template --template "${BASE_TEMPLATE:?indicar plantilla de 8f51e8d}"
```

Con un perfil de lectura de la cuenta de pruebas, omitir `--template` permite comparar con CloudFormation. Mantener `--method=template` evita crear change sets. STS, Policy Simulator, la asociación efectiva y la ejecución en AWS quedan pendientes de acceso a esa cuenta y de aprobación para cambios.

## Evidencia local

El 26 de noviembre de 2026, la suite completa de Connector pasó con 112 pruebas y 12 subpruebas en 7,20 s, usando las dependencias de `connector/requirements.txt`. Los 17 casos específicos incluyen permisos denegados, recuperación de nombres existentes, origen del claim y renovación del proveedor de credenciales. Un primer ensayo del test del loop agotó su timeout al usar el executor real. Se aisló ese worker con un mock y se conservó la comprobación de que AccessDenied produce RETRY.

La síntesis y el diff local contra `8f51e8d` finalizaron correctamente. Cambia exclusivamente `ConnectorSagaRoleDefaultPolicyDF593B69`. Los recursos y outputs conservan sus identificadores y las plantillas de ExistingInfrastructure y Monitoring coinciden byte por byte con la base. El escáner de secretos informó cero hallazgos en los cuatro archivos revisados de implementación, prueba y guía.

El entorno carece de AWS CLI y de una cuenta de pruebas seleccionada. Las comprobaciones STS, Policy Simulator y diff remoto permanecen pendientes. El despliegue y la asociación del perfil al host requieren aprobación explícita.

La regresión completa de IaC, ASL y Lambda terminó con `Ran 151 tests in 44.342s` y `OK`, con timeout de 420 s. Incluye pruebas de alcance allow/deny y trust policy sobre la plantilla sintetizada. El SHA-256 de la plantilla de Sagas es `e41b9d565521e6fe098bdf12fa1d2b295edff0d2b22082d127537e82c0438148`. `git diff --check` terminó sin errores.
