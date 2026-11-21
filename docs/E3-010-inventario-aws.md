# Inventario AWS de E2 y estimación preliminar para E3

Este documento resume los recursos AWS identificados al cierre de E2. Corresponde a un inventario de referencia y no acredita por sí solo su estado operativo actual.

## Inventario

| Servicio | Estado observado |
| --- | --- |
| EC2 | Instancia `t3.micro` activa, con volumen EBS `gp3` de 20 GiB sin cifrar. |
| ECR | Repositorio `energyshark-backend`, cifrado AES256 y escaneo de imágenes activado. |
| API Gateway | HTTP API con integraciones `HTTP_PROXY`. |
| S3 | Bucket `energyshark-g10-frontend`, con lectura pública. |
| CloudFront | Distribución `EFUBNHG9ZB6SV`, OAC con SigV4 y sin WAF asociado. |
| IAM | La instancia EC2 no tiene un rol asociado; no se encontraron roles propios del proyecto. |
| CloudWatch | No se encontraron alarmas de métricas. La integración con New Relic no fue verificada. |
| Budgets | Presupuestos de USD 5 y USD 1, con cuatro alertas y destinatarios por correo. Ambos incluyen Step Functions y Lambda. |
| Step Functions | Sin máquinas de estado desplegadas todavía. |
| Lambda | Sin funciones desplegadas todavía. |

## Estimación preliminar de E3

Para dimensionar la incorporación de Step Functions y Lambda se asumen 72 ciclos diarios, 2 swaps por ciclo, 12 transiciones de estado y 4 invocaciones Lambda por swap.

Con esa carga, la estimación preliminar es de aproximadamente **USD 1,45 mensuales sin capa gratuita** y **USD 1,20 mensuales con capa gratuita**. Es una referencia de planificación: excluye EC2, EBS, transferencia de datos, logs, API Gateway, almacenamiento, reintentos, mayor duración de funciones y cualquier uso adicional.

## Hallazgos para revisión de seguridad

Como posibles hallazgos de Prowler deben revisarse el volumen EBS sin cifrar y el acceso público al bucket S3. La validación definitiva dependerá del análisis de Prowler y del contexto de exposición requerido para el frontend.
