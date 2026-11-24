#!/usr/bin/env bash
# E3-075: reescaneo enfocado con Prowler para comparar contra la línea base.
#
# Usa la imagen oficial toniblyx/prowler:5.50.0 en Docker y los mismos siete
# checks del escaneo del 23/11/2026 02:40 UTC, en us-east-1 y us-east-2.
#
# Uso:
#   tools/prowler_scan_e3.sh --profile arquisis --out ~/prowler-e3/after-2026-11-23

set -euo pipefail

PROFILE=""
OUT=""
CHECKS=(
  s3_bucket_public_access
  s3_bucket_level_public_access_block
  s3_account_level_public_access_blocks
  iam_user_mfa_enabled_console_access
  ec2_ebs_default_encryption
  s3_bucket_secure_transport_policy
  s3_bucket_policy_public_write_access
)

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile) PROFILE="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "Argumento desconocido: $1" >&2; exit 2 ;;
  esac
done

if [[ -z "$OUT" ]]; then
  echo "Falta --out" >&2
  exit 2
fi

mkdir -p "$OUT"

AWS=(aws)
[[ -n "$PROFILE" ]] && AWS+=(--profile "$PROFILE")

eval "$("${AWS[@]}" configure export-credentials --format env)"

docker run --rm \
  -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY -e AWS_SESSION_TOKEN \
  -v "${OUT}:/output" \
  toniblyx/prowler:5.50.0 aws \
    -c "${CHECKS[@]}" \
    -f us-east-1 us-east-2 \
    -M csv json-ocsf \
    -o /output

echo "Reporte en: $OUT"
