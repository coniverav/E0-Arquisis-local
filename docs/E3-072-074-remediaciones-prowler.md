# E3-072, E3-073 y E3-074 · Remedición de tres hallazgos Prowler

Cambios aplicados sobre la cuenta `521294961974` el 23/11/2026 a las 16:51 UTC, con la sesión `arn:aws:iam::521294961974:root`. Evidencia fuera del repositorio en `~/prowler-e3/evidence/20261123-165132/`.

Ejecución en orden `E3-073` → `E3-074` → `E3-072`: primero se quita la sentencia pública de la política, después se activa el bloqueo y al final el cifrado. Ese orden evita que `BlockPublicPolicy` rechace la política todavía pública.

Los tres cambios se ejecutaron con un único script reproducible:

```bash
tools/prowler_remediate_e3.sh --profile arquisis --apply
```

Por defecto el script es dry-run. Guarda el estado previo, el estado posterior y la respuesta HTTP de CloudFront.

---

## E3-072 · Cifrado EBS predeterminado (`ec2_ebs_default_encryption`, high)

**Riesgo:** los volúmenes EBS nuevos se creaban sin cifrar en `us-east-1` y `us-east-2`. El volumen de 20 GiB del host productivo ya existe y no está cifrado; este check mide solo la configuración por región.

**Cambio:**

```bash
aws ec2 enable-ebs-encryption-by-default --region us-east-2
aws ec2 enable-ebs-encryption-by-default --region us-east-1
```

**Antes / después:**

| Región | Antes | Después | Evidencia |
| :--- | :--- | :--- | :--- |
| `us-east-2` | `False` | `True` | `ebs-us-east-2-before.txt` |
| `us-east-1` | `False` | `True` | `ebs-us-east-1-before.txt` |

**Impacto:** solo aplica a volúmenes creados después. No modifica el volumen existente y no reinicia la instancia.

**Rollback:**

```bash
aws ec2 disable-ebs-encryption-by-default --region us-east-2
aws ec2 disable-ebs-encryption-by-default --region us-east-1
```

---

## E3-073 · Acceso público al bucket S3 (`s3_bucket_public_access`, critical)

**Riesgo:** la sentencia `PublicReadGetObject` permitía `s3:GetObject` a `Principal: "*"` sobre `arn:aws:s3:::energyshark-g10-frontend/*`. Cualquiera en Internet podía leer los objetos del frontend sin pasar por CloudFront.

**Por qué es seguro quitarla:** la distribución `EFUBNHG9ZB6SV` usa origen con OAC (`E3USAWK540ITSR`) y ya existía la sentencia `AllowCloudFrontServicePrincipal`. El bucket no tiene website configuration y ningún componente del código referencia la URL de S3 directamente.

**Cambio:** se elimina la sentencia pública y se agrega `DenyInsecureTransport`, que cierra también el hallazgo `s3_bucket_secure_transport_policy` (medium).

La política resultante queda con dos sentencias:

| Sid | Efecto | Principal | Acción |
| :--- | :--- | :--- | :--- |
| `AllowCloudFrontServicePrincipal` | Allow | `cloudfront.amazonaws.com` | `s3:GetObject` |
| `DenyInsecureTransport` | Deny | `*` | `s3:*` cuando `aws:SecureTransport` es `false` |

**Antes / después:**

| Prueba | Antes | Después |
| :--- | :--- | :--- |
| `https://d9yjiq237jfab.cloudfront.net/` | 200 | 200 |
| `https://d9yjiq237jfab.cloudfront.net/index.html` | 200 | 200 |
| `https://energyshark-g10-frontend.s3.amazonaws.com/index.html` (anónimo) | 200 | 403 |
| `http://energyshark-g10-frontend.s3.amazonaws.com/index.html` (sin TLS) | 200 | 403 |

Evidencia: `s3-policy-before.json`, `s3-policy-after.json`, `cloudfront-before.txt`, `verify-after.txt`.

El sitio sigue sirviéndose por CloudFront con el `index.html` real (`<!doctype html>`, 468 bytes).

**Impacto:** se pierde el acceso directo por URL de S3. CloudFront no se ve afectado.

**Rollback:**

```bash
aws s3api put-bucket-policy --bucket energyshark-g10-frontend \
  --policy file://~/prowler-e3/evidence/20261123-165132/s3-policy-before.json
```

---

## E3-074 · Bloqueo de acceso público del bucket (`s3_bucket_level_public_access_block`, high)

**Riesgo:** con las cuatro opciones en `false`, cualquier cambio futuro de ACL o de política podía volver público el bucket sin fricción.

**Cambio:**

```bash
aws s3api put-public-access-block --bucket energyshark-g10-frontend \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
```

**Antes / después:**

| Opción | Antes | Después |
| :--- | :--- | :--- |
| `BlockPublicAcls` | `false` | `true` |
| `IgnorePublicAcls` | `false` | `true` |
| `BlockPublicPolicy` | `false` | `true` |
| `RestrictPublicBuckets` | `false` | `true` |

Evidencia: `s3-block-before.json` y la salida de `get-public-access-block` en `verify-after.txt`.

**Impacto:** bloquea ACL públicas y políticas públicas futuras. CloudFront con OAC sigue funcionando, porque su acceso no depende de una política pública.

**Rollback:**

```bash
aws s3api delete-public-access-block --bucket energyshark-g10-frontend
```

---

## Verificación conjunta

Tras los tres cambios:

- CloudFront responde 200 en `/` e `/index.html`.
- El acceso anónimo directo a S3 responde 403 por HTTPS y por HTTP plano.
- `get-public-access-block` devuelve las cuatro opciones en `true`.
- `get-ebs-encryption-by-default` devuelve `True` en `us-east-1` y `us-east-2`.

La comprobación formal con Prowler está en `docs/E3-075-prowler-comparativa.md`.

## Alcance respetado

Solo se modificaron los tres recursos de estos hallazgos. No se tocó código, infraestructura ni tareas de otros integrantes (`E3-014` a `E3-031` y siguientes).

## Pendientes declarados

- `iam_user_mfa_enabled_console_access` (high): requiere el dispositivo del usuario `grupo10`.
- `s3_account_level_public_access_blocks` (high): afecta a toda la cuenta; conviene aplicarlo después de revisar otros consumidores.
- La sesión usada fue `root`. Para futuras remediaciones conviene un usuario IAM con `SecurityAudit`.
