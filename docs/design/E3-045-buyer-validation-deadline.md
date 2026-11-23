# E3-045 — Verificar swapToken ES256/JWKS y deadline comprador

- La Lambda `validate-buyer-token` recupera `swapToken` **únicamente** desde `swap_records`, cruzando `saga_executions` y `market_bids` por la identidad durable de E3-044.
- JWT ES256, `kid` EC P-256, `iss`, `sub`, `proposer`, `give`, `take`, `memo`, `iat` y `exp` son validados contra una JWKS HTTPS configurada en el secreto de Wallet. La URL predeterminada sale de la raíz HTTPS del origen de `baseUrl`, no de `/v1`.
- La verificación compara además el `exp` firmado con `swap_records.expires_at` (tolerancia de 1 s por precisión Unix); mismatches, tokens expirados y firmas inválidas detienen el avance.
- El JWT completo **nunca** viaja en input/resultados de Step Functions ni se registra en logs. El resultado informa solo `verified`, `swapId` e identidad de saga.
- `check-buyer-deadline` comprueba el menor plazo entre `closesAt` y `expiresAt`, con margen de seguridad mínimo de tres segundos. No requiere BD ni Wallet.
- La imagen Lambda incorpora PyJWT y cryptography; solo la tarea de validación recibe permisos para el secreto de Wallet. Errores de red/JWKS reintentables se distinguen de JWT inválidos.
- No se efectúa aceptación económica en E3-045. El procesamiento y callbacks efectivos corresponden a E3-046 y siguientes.

## Límites

Estas pruebas locales ejercitan PostgreSQL y la firma JWT sin llamadas reales a AWS; los flags E3 siguen apagados hasta el end-to-end. E2 y Lambda mantienen verificadores separados por el build context de CDK, por lo que cualquier cambio posterior en el contrato JWT debe revisarse en ambos adaptadores.
