# E3-075 · Reejecución de Prowler y comparativa

Segundo escaneo con **Prowler 5.50.0** (`toniblyx/prowler:5.50.0`) sobre la cuenta `521294961974`, regiones `us-east-1` y `us-east-2`, el 23/11/2026 a las 19:53 UTC. Mismos siete checks que la línea base, para que la comparación sea uno a uno.

| Reporte | Fecha | Archivo |
| :--- | :--- | :--- |
| Línea base | 23/11/2026 02:40 UTC | `~/prowler-e3/baseline-2026-11-22/prowler-output-521294961974-20261123024004.ocsf.json` |
| Posterior | 23/11/2026 19:53 UTC | `~/prowler-e3/after-2026-11-23/prowler-output-521294961974-20261123195305.ocsf.json` |

Reproducible con:

```bash
tools/prowler_scan_e3.sh --profile arquisis --out ~/prowler-e3/after-2026-11-23
python3 tools/prowler_report.py compare <baseline.ocsf.json> <after.ocsf.json>
```

Los reportes viven fuera del repositorio. No se versionan credenciales ni datos de cuenta.

## Resultado por check

| Check ID | Severidad | Antes | Después | Resultado |
| :--- | :--- | :--- | :--- | :--- |
| `s3_bucket_public_access` | critical | FAIL | PASS | corregido |
| `s3_bucket_level_public_access_block` | high | FAIL | PASS | corregido |
| `ec2_ebs_default_encryption` | high | FAIL | PASS | corregido |
| `s3_bucket_secure_transport_policy` | medium | FAIL | PASS | corregido |
| `s3_bucket_policy_public_write_access` | critical | PASS | PASS | sin cambio |
| `iam_user_mfa_enabled_console_access` | high | FAIL | FAIL | pendiente |
| `s3_account_level_public_access_blocks` | high | FAIL | FAIL | pendiente |

Resumen: **7 checks comparados · 4 corregidos · 0 regresiones · 2 pendientes declarados**.

Cuatro hallazgos quedan resueltos: los tres elegidos en `E3-071` más `s3_bucket_secure_transport_policy`, que se cerró como efecto de la sentencia `DenyInsecureTransport` agregada en `E3-073`.

## Trazabilidad de cada corrección

| Check | Cambio aplicado | Documento |
| :--- | :--- | :--- |
| `s3_bucket_public_access` | Se eliminó `PublicReadGetObject` de la política del bucket | E3-073 |
| `s3_bucket_secure_transport_policy` | Se agregó `DenyInsecureTransport` con `aws:SecureTransport: false` | E3-073 |
| `s3_bucket_level_public_access_block` | Las cuatro opciones de bloqueo público en `true` | E3-074 |
| `ec2_ebs_default_encryption` | Cifrado predeterminado activo en `us-east-1` y `us-east-2` | E3-072 |

Detalle, evidencia antes/después y rollback: `docs/E3-072-074-remediaciones-prowler.md`.

## Verificación funcional

El sitio sigue operativo después de los cambios:

| Prueba | Resultado |
| :--- | :--- |
| `https://d9yjiq237jfab.cloudfront.net/` | 200, `index.html` real |
| Acceso anónimo directo a S3 por HTTPS | 403 |
| Acceso directo a S3 por HTTP plano | 403 |
| `get-public-access-block` | cuatro opciones en `true` |
| `get-ebs-encryption-by-default` (`us-east-1`, `us-east-2`) | `True` |

## Hallazgos no corregidos

| Check | Motivo | Siguiente paso |
| :--- | :--- | :--- |
| `iam_user_mfa_enabled_console_access` | El usuario `grupo10` no tiene dispositivos MFA. Requiere el teléfono del titular. | Registrar el dispositivo y repetir el escaneo. |
| `s3_account_level_public_access_blocks` | Afecta a toda la cuenta, no solo al bucket del frontend. | Revisar otros consumidores de S3 antes de aplicarlo. |

Ambos quedan fuera del alcance de `E3-072`–`E3-074` y están declarados, no ocultos.

## Limitaciones de esta comparativa

- El reporte original del 17/11/2026 (`docs/E3-011-prowler-baseline.md`) no estaba disponible en el equipo, así que la línea base es un escaneo nuevo del 23/11/2026 02:40 UTC, previo a cualquier cambio. La comparación es válida porque ambos escaneos usan la misma imagen, los mismos siete checks y las mismas regiones.
- El escaneo es enfocado: cubre los siete checks de los tres candidatos, no la totalidad de servicios de la cuenta.
- Los dos escaneos se ejecutaron con la identidad `root`. Eso no altera el resultado de los checks, pero conviene migrar a un usuario IAM con `SecurityAudit` para próximas remediaciones.

## Criterio de cierre cumplido

`E3-075` pide que el informe posterior acredite la remediación de tres hallazgos y que los originales queden archivados, no solo un conteo global. Aquí se cumple: la tabla anterior muestra el estado por check y por recurso antes y después, los reportes completos están archivados y cada corrección tiene su evidencia.
