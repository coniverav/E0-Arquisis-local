#!/usr/bin/env bash
# E3-071..E3-074: diagnóstico read-only de los tres candidatos del baseline Prowler.
#
# No modifica recursos. Solo imprime el estado actual para dejar evidencia
# "antes" de cada remediación y repetirla después.
#
# Uso:
#   tools/prowler_diagnose_e3.sh --profile arquisis
#   tools/prowler_diagnose_e3.sh --profile arquisis --bucket energyshark-g10-frontend --user grupo10

set -euo pipefail

PROFILE=""
BUCKET="energyshark-g10-frontend"
IAM_USER="grupo10"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile) PROFILE="$2"; shift 2 ;;
    --bucket) BUCKET="$2"; shift 2 ;;
    --user) IAM_USER="$2"; shift 2 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "Argumento desconocido: $1" >&2; exit 2 ;;
  esac
done

AWS=(aws)
if [[ -n "$PROFILE" ]]; then
  AWS+=(--profile "$PROFILE")
fi

section() { printf '\n===== %s =====\n' "$1"; }

section "Identidad y cuenta"
"${AWS[@]}" sts get-caller-identity --query '{Account:Account,Arn:Arn}' --output json

section "072 · ec2_ebs_default_encryption"
"${AWS[@]}" ec2 get-ebs-encryption-by-default \
  --query '{EbsEncryptionByDefault:EbsEncryptionByDefault}' --output json

section "073 · s3_bucket_public_access_block (${BUCKET})"
"${AWS[@]}" s3api get-public-access-block --bucket "$BUCKET" --output json

section "073 · política actual del bucket (${BUCKET})"
"${AWS[@]}" s3api get-bucket-policy --bucket "$BUCKET" --output text 2>/dev/null \
  || echo "Sin política de bucket asociada"

section "074 · iam_user_mfa_enabled (${IAM_USER})"
"${AWS[@]}" iam list-mfa-devices --user-name "$IAM_USER" --output json
"${AWS[@]}" iam get-login-profile --user-name "$IAM_USER" \
  --query '{HasConsolePassword:true,CreateDate:CreateDate}' --output json 2>/dev/null \
  || echo "El usuario no tiene perfil de consola"

printf '\nGuardar esta salida como evidencia "antes". Repetir tras cada cambio.\n'
