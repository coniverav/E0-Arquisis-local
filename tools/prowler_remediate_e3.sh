#!/usr/bin/env bash
# E3-072..E3-074: remediación de los tres hallazgos Prowler seleccionados.
#
# Por defecto solo muestra el plan (dry-run). Con --apply ejecuta los cambios,
# guarda el estado previo y verifica CloudFront después de tocar S3.
#
# Uso:
#   tools/prowler_remediate_e3.sh --profile arquisis
#   tools/prowler_remediate_e3.sh --profile arquisis --apply

set -euo pipefail

PROFILE=""
BUCKET="energyshark-g10-frontend"
DISTRIBUTION="EFUBNHG9ZB6SV"
APPLY=0
EVIDENCE_DIR="${HOME}/prowler-e3/evidence/$(date +%Y%m%d-%H%M%S)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile) PROFILE="$2"; shift 2 ;;
    --bucket) BUCKET="$2"; shift 2 ;;
    --distribution) DISTRIBUTION="$2"; shift 2 ;;
    --apply) APPLY=1; shift ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "Argumento desconocido: $1" >&2; exit 2 ;;
  esac
done

AWS=(aws)
[[ -n "$PROFILE" ]] && AWS+=(--profile "$PROFILE")

run() {
  if [[ $APPLY -eq 1 ]]; then
    "$@"
  else
    printf '[dry-run] %s\n' "$*"
  fi
}

section() { printf '\n===== %s =====\n' "$1"; }

mkdir -p "$EVIDENCE_DIR"
echo "Evidencia: $EVIDENCE_DIR"

section "Identidad"
"${AWS[@]}" sts get-caller-identity --output json | tee "$EVIDENCE_DIR/identity.json"

section "Estado previo"
"${AWS[@]}" ec2 get-ebs-encryption-by-default --region us-east-2 \
  --query EbsEncryptionByDefault --output text | tee "$EVIDENCE_DIR/ebs-us-east-2-before.txt"
"${AWS[@]}" ec2 get-ebs-encryption-by-default --region us-east-1 \
  --query EbsEncryptionByDefault --output text | tee "$EVIDENCE_DIR/ebs-us-east-1-before.txt"
"${AWS[@]}" s3api get-public-access-block --bucket "$BUCKET" \
  --output json > "$EVIDENCE_DIR/s3-block-before.json" || true
"${AWS[@]}" s3api get-bucket-policy --bucket "$BUCKET" --query Policy --output text \
  > "$EVIDENCE_DIR/s3-policy-before.json"

DOMAIN=$("${AWS[@]}" cloudfront get-distribution --id "$DISTRIBUTION" \
  --query 'Distribution.DomainName' --output text)
echo "CloudFront: $DOMAIN"
curl -sS -o /dev/null -w 'http_antes=%{http_code}\n' "https://${DOMAIN}/" \
  | tee "$EVIDENCE_DIR/cloudfront-before.txt"

section "073 · política S3 sin acceso público"
python3 "$(dirname "$0")/prowler_s3_policy.py" \
  "$EVIDENCE_DIR/s3-policy-before.json" "$EVIDENCE_DIR/s3-policy-after.json"

run "${AWS[@]}" s3api put-bucket-policy --bucket "$BUCKET" \
  --policy "file://$EVIDENCE_DIR/s3-policy-after.json"

section "074 · bloqueo de acceso público del bucket"
run "${AWS[@]}" s3api put-public-access-block --bucket "$BUCKET" \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

section "072 · cifrado EBS predeterminado"
run "${AWS[@]}" ec2 enable-ebs-encryption-by-default --region us-east-2
run "${AWS[@]}" ec2 enable-ebs-encryption-by-default --region us-east-1

section "Verificación"
if [[ $APPLY -eq 1 ]]; then
  "${AWS[@]}" ec2 get-ebs-encryption-by-default --region us-east-2 --query EbsEncryptionByDefault --output text
  "${AWS[@]}" ec2 get-ebs-encryption-by-default --region us-east-1 --query EbsEncryptionByDefault --output text
  "${AWS[@]}" s3api get-public-access-block --bucket "$BUCKET" --output json
  curl -sS -o /dev/null -w 'http_despues=%{http_code}\n' "https://${DOMAIN}/"
else
  echo "Dry-run: no se modificó ningún recurso."
fi

cat <<EOF

Rollback:
  073  aws s3api put-bucket-policy --bucket $BUCKET --policy file://$EVIDENCE_DIR/s3-policy-before.json
  074  aws s3api delete-public-access-block --bucket $BUCKET
  072  aws ec2 disable-ebs-encryption-by-default --region us-east-2
       aws ec2 disable-ebs-encryption-by-default --region us-east-1
EOF
