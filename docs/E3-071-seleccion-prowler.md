# E3-071 · Selección de tres hallazgos Prowler

Escaneo enfocado con **Prowler 5.50.0** (imagen oficial `toniblyx/prowler:5.50.0`) sobre la cuenta `521294961974`, regiones `us-east-1` y `us-east-2`, el 23/11/2026 a las 02:40 UTC. Reporte en `~/prowler-e3/baseline-2026-11-22/` (JSON OCSF y CSV), fuera del repositorio.

El reporte completo del 17/11/2026 (`docs/E3-011-prowler-baseline.md`) no estaba disponible en el equipo de trabajo, así que este escaneo enfocado se usa como línea base fechada. Los siete checks elegidos corresponden a los tres candidatos registrados entonces: cifrado EBS, MFA de usuario IAM y acceso público de S3.

## Resultado del escaneo

| Check | Severidad | Recurso | Estado |
| :--- | :--- | :--- | :--- |
| `s3_bucket_public_access` | critical | `arn:aws:s3:::energyshark-g10-frontend` | FAIL |
| `s3_bucket_level_public_access_block` | high | `arn:aws:s3:::energyshark-g10-frontend` | FAIL |
| `s3_account_level_public_access_blocks` | high | `arn:aws:s3:us-east-1:521294961974:account` | FAIL |
| `iam_user_mfa_enabled_console_access` | high | `arn:aws:iam::521294961974:user/grupo10` | FAIL |
| `ec2_ebs_default_encryption` | high | `arn:aws:ec2:us-east-2:521294961974:volume` | FAIL |
| `s3_bucket_secure_transport_policy` | medium | `arn:aws:s3:::energyshark-g10-frontend` | FAIL |
| `s3_bucket_policy_public_write_access` | critical | `arn:aws:s3:::energyshark-g10-frontend` | PASS |

Total: 6 fallidos, 1 aprobado.

## Hallazgos seleccionados

Se eligen los tres corregibles sin depender de un dispositivo físico del usuario y con rollback verificable.

### 1. `s3_bucket_public_access` · critical

- **Riesgo:** cualquier persona en Internet puede leer los objetos del bucket del frontend (`s3:GetObject` con `Principal: "*"`). Expone el código y los assets publicados.
- **Corrección:** eliminar la sentencia `PublicReadGetObject` de la política del bucket. CloudFront ya usa OAC (`E3USAWK540ITSR`) y el bucket no tiene website configuration, así que el acceso público directo es innecesario.
- **Impacto:** el sitio sigue sirviéndose por CloudFront; se pierde el acceso directo por URL de S3.
- **Rollback:** restaurar la política guardada en `~/prowler-e3/evidence/s3-policy-before.json`.
- **Evidencia:** política antes/después y respuesta HTTP de la distribución antes/después.

### 2. `s3_bucket_level_public_access_block` · high

- **Riesgo:** sin bloqueo, cualquier cambio futuro de ACL o política puede volver público el bucket sin fricción.
- **Corrección:** `PutPublicAccessBlock` con las cuatro opciones en `true`.
- **Impacto:** bloquea ACL y políticas públicas futuras; no afecta a CloudFront con OAC.
- **Rollback:** reponer la configuración previa (las cuatro en `false`).

### 3. `ec2_ebs_default_encryption` · high

- **Riesgo:** los volúmenes EBS nuevos se crean sin cifrar. El volumen actual de 20 GiB del host productivo tampoco está cifrado, pero el check mide la configuración por región.
- **Corrección:** habilitar el cifrado predeterminado en `us-east-1` y `us-east-2`.
- **Impacto:** solo aplica a volúmenes creados después; no modifica el volumen existente ni reinicia la instancia.
- **Rollback:** `disable-ebs-encryption-by-default` por región.

## No seleccionados

| Check | Motivo |
| :--- | :--- |
| `iam_user_mfa_enabled_console_access` | Requiere el dispositivo del usuario `grupo10`; queda como candidato para E3-074 si se dispone de él. |
| `s3_account_level_public_access_blocks` | Cambio de cuenta completa; se evalúa después de corregir el bucket para no romper otros consumidores. |
| `s3_bucket_secure_transport_policy` | Se corrige junto con el hallazgo 1 añadiendo una sentencia que niega HTTP sin TLS. |

## Criterio de cierre

Los tres hallazgos quedan cerrados cuando el escaneo posterior muestra `PASS` para `s3_bucket_public_access`, `s3_bucket_level_public_access_block` y `ec2_ebs_default_encryption`, con evidencia antes/después y política anterior archivada. La comparación se documenta en E3-075.
