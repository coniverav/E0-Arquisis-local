# Baseline inicial de seguridad con Prowler

El 17/11/2026 se realizó el escaneo inicial de la cuenta AWS con **Prowler 5.50.0**. Esta ejecución establece la línea base de seguridad previa a las remediaciones de E3.

## Resultados

| Métrica | Cantidad |
| --- | ---: |
| Checks ejecutados | 671 |
| Hallazgos fallidos | 258 |
| Hallazgos aprobados | 177 |
| Críticos | 4 |
| Altos | 33 |
| Medios | 116 |
| Bajos | 105 |

## Evidencia

Prowler generó reportes en formatos CSV, HTML y JSON OCSF. La evidencia completa se conserva únicamente en el entorno local:

`~/prowler-e3/baseline-2026-11-17/`

Los reportes no se incorporan al repositorio porque pueden contener identificadores y detalles sensibles de la cuenta AWS.

## Candidatos de remediación

Se identificaron tres candidatos iniciales para revisión y corrección posterior:

1. Habilitar el cifrado EBS predeterminado.
2. Habilitar MFA para el usuario IAM `grupo10`.
3. Revisar y restringir el acceso público del bucket S3.

Este documento solo registra el baseline. Todavía no se aplicaron correcciones ni cambios de infraestructura; las remediaciones y su validación corresponden a tareas posteriores.
