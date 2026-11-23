# E3-076 · Stack E1/E2 reproducible en IaC

La infraestructura E2 deja de estar hecha a mano. El stack `ExistingInfrastructure` del CDK declara red, cómputo, frontend y API pública desde cero, y el stack `Monitoring` declara las alarmas, el topic SNS y el panel. Ningún recurso se importa: una cuenta limpia puede crear todo con `cdk deploy` sin tocar recursos existentes.

## Punto de partida

En `develop`, `infra/cdk/energyshark_infra/stacks/existing_infrastructure.py` y `monitoring.py` eran límites vacíos: el test `test_unmigrated_stack_boundaries_remain_empty` exigía que ambos stacks no declararan recursos. La tabla de rutas del API tampoco existía.

## Qué declara ahora

### Red

| Recurso | Configuración |
| :--- | :--- |
| VPC | `10.20.0.0/16` por defecto, DNS habilitado |
| Subredes | Tres públicas en tres AZ, `MapPublicIpOnLaunch` activo |
| Internet gateway | Uno, con ruta `0.0.0.0/0` |
| NAT | Ninguno |
| Grupo de seguridad | 80 y 443 abiertos a Internet, 22 solo si `adminCidr` se define |
| Grupo de seguridad por defecto | Restringido por CDK |

### Cómputo

| Recurso | Configuración |
| :--- | :--- |
| Instancia | `t3.micro`, AMI fijada por contexto, par de llaves `arquisisg10` |
| IP elástica | Asociada a la instancia |
| Volumen raíz | 20 GiB `gp3` cifrado |
| Metadatos | IMDSv2 obligatorio |
| Rol IAM | `AmazonSSMManagedInstanceCore` y `CloudWatchAgentServerPolicy` |
| User data | Instala Docker y levanta `docker-compose.prod.yml` si hay `appRepositoryUrl` |

### Frontend

| Recurso | Configuración |
| :--- | :--- |
| Bucket | Privado, AES256 con bucket key, TLS obligatorio |
| Bloqueo público | Las cuatro opciones en `true` |
| CloudFront | Origin Access Control, `index.html` por defecto |
| Protocolo | `redirect-to-https`, HTTP/2, compresión activa |
| Errores | 403 y 404 reescritos a `/index.html` con código 200 |
| Alias | Solo si se configuran `frontendDomainName` y `certificateArn` |

### API pública

| Recurso | Configuración |
| :--- | :--- |
| API Gateway | HTTP API |
| Rutas | 29, versionadas en `energyshark_infra/api_routes.py` |
| Autorizador | JWT de Auth0 en las 10 rutas protegidas |
| Integraciones | Proxy HTTP a `apiBackendUrl` con prefijo `/api` |
| CORS | Orígenes configurables, métodos GET/POST/PATCH/OPTIONS |
| Stage | `$default` con auto deploy |
| Logs de acceso | Grupo de CloudWatch con retención parametrizada |

La tabla de rutas se reconstruyó desde el API real `ayv7o6rl9l` con `aws apigatewayv2 export-api --specification OAS30`. Las 29 rutas, sus métodos y sus 10 rutas con JWT coinciden con el inventario.

### Observabilidad

| Recurso | Configuración |
| :--- | :--- |
| Alarmas | 5xx del API, latencia p95 mayor a 2 s, volumen de 4xx, estado de la instancia, CPU mayor a 80 % |
| Topic SNS | Uno, con suscripción por correo opcional |
| Panel | Peticiones, errores, latencia y estado del host |

Las cinco alertas de negocio de E2-061 (wallet, broker, market) viven en New Relic y se recrean con `tools/newrelic_e2061.py`. CloudFormation no puede declararlas; el README lo aclara para no duplicar monitoreo.

## Evidencia de validación

```
$ CDK_OUTDIR=cdk.out .venv/bin/python app.py
$ ls cdk.out/*.template.json
EnergySharkDev-ExistingInfrastructure.template.json
EnergySharkDev-Monitoring.template.json
EnergySharkDev-Sagas.template.json

$ aws cloudformation validate-template \
    --template-body file://cdk.out/EnergySharkDev-ExistingInfrastructure.template.json
1        # parámetro declarado, sin errores de esquema

$ aws cloudformation validate-template \
    --template-body file://cdk.out/EnergySharkDev-Monitoring.template.json
1
```

Recursos que genera el stack E2 en `dev`: 90, entre VPC, subredes, instancia, IP elástica, bucket, distribución, 29 rutas, 29 integraciones, autorizador, stage y grupo de logs.

```
$ python -m unittest discover -s tests
Ran 113 tests
FAILED (failures=1, errors=2)
```

Los tres fallos son previos a este cambio y existen igual en `develop`: `test_e3045_buyer_validation` (import), `test_handler_fails_closed_without_logging_sensitive_payload` y `test_buyer_bad_token_never_calls_wallet`. Este cambio agrega 11 tests nuevos en `tests/test_e3076_reproducible_stack.py` y actualiza la expectativa de `tests/test_app.py`: los tres stacks ahora declaran recursos.

## Cómo se verifica

```bash
cd infra/cdk
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest tests.test_e3076_reproducible_stack -v
CDK_OUTDIR=cdk.out .venv/bin/python app.py
```

Los tests cubren:

- Red pública de tres subredes, sin NAT.
- Puertos 80 y 443 abiertos; 22 cerrado por defecto y abierto solo con `adminCidr`.
- Instancia con IP elástica, volumen cifrado, IMDSv2 y rol de Session Manager.
- Bucket privado, cifrado y con política TLS.
- CloudFront con OAC y reescritura de 403/404.
- Las 29 rutas, sus integraciones y el autorizador JWT.
- Stage con auto deploy y logs de acceso.
- Cinco alarmas, topic y panel.
- Endurecimiento en `prod`: alias, certificado, SSH acotado, correo de alertas y `DeletionPolicy: Retain`.
- Síntesis determinista: dos ejecuciones producen el mismo template.

## Límites declarados

- **No se desplegó nada.** El DoD de E3-076 es que las plantillas existan y sinteticen; el ensayo aislado corresponde a E3-078.
- El stack de sagas ya existía y no se modificó. Su template supera el límite de 51 200 bytes de `validate-template`; se valida con sus tests y con un change set en cuenta aislada.
- La validación de confianza del rol de instancia y de los secretos IaC es E3-077.
- El DNS de `energyshark-g10.tech` es externo a la cuenta: no hay zona hospedada en Route 53, así que el alias y el certificado se reciben por contexto.
- El bucket no activa versionado, igual que el bucket productivo actual. Queda como pendiente de endurecimiento para E3-077.
- El AMI se fija por contexto porque resolver la última AMI requiere credenciales y haría la síntesis no determinista.

## Archivos

- `infra/cdk/energyshark_infra/api_routes.py`: tabla de las 29 rutas.
- `infra/cdk/energyshark_infra/config.py`: parámetros del stack E2 y retención de logs.
- `infra/cdk/energyshark_infra/stacks/existing_infrastructure.py`: red, cómputo, frontend y API.
- `infra/cdk/energyshark_infra/stacks/monitoring.py`: alarmas, SNS y panel.
- `infra/cdk/app.py`: conecta los stacks y comparte API e instancia con monitoreo.
- `infra/cdk/tests/test_e3076_reproducible_stack.py`: 11 tests nuevos.
- `infra/cdk/tests/test_app.py`: expectativa actualizada.
- `infra/cdk/README.md`: estructura, claves de contexto y validación.
