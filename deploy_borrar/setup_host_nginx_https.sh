#!/usr/bin/env bash
set -euo pipefail

# Uso futuro en EC2:
#   sudo bash deploy/setup_host_nginx_https.sh dominio.me correo@ejemplo.com
# Requisitos previos: dominio apuntando a EC2 y puertos 80/443 abiertos.

if [[ $# -ne 2 ]]; then
  echo "Uso: $0 <dominio> <email>"
  exit 1
fi

DOMAIN="$1"
EMAIL="$2"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

apt-get update
apt-get install -y nginx certbot
mkdir -p /var/www/certbot

# 1) Nginx HTTP con las dos réplicas para superar el challenge inicial.
sed "s/__DOMAIN__/${DOMAIN}/g" \
  "$PROJECT_DIR/deploy/nginx/energyshark-http.conf.template" \
  > /etc/nginx/sites-available/energyshark
ln -sf /etc/nginx/sites-available/energyshark /etc/nginx/sites-enabled/energyshark
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

# 2) Certificado real Let's Encrypt mediante HTTP-01.
certbot certonly \
  --webroot -w /var/www/certbot \
  -d "$DOMAIN" -d "www.$DOMAIN" \
  --non-interactive --agree-tos -m "$EMAIL"

# 3) Activa HTTPS y redirección HTTP -> HTTPS.
sed "s/__DOMAIN__/${DOMAIN}/g" \
  "$PROJECT_DIR/deploy/nginx/energyshark-https.conf.template" \
  > /etc/nginx/sites-available/energyshark
nginx -t
systemctl reload nginx

# 4) Requisito del enunciado: comprobar renovación DOS veces al día.
cat > /etc/cron.d/energyshark-certbot-renew <<'EOF'
0 0,12 * * * root certbot renew --quiet --deploy-hook "systemctl reload nginx"
EOF
chmod 644 /etc/cron.d/energyshark-certbot-renew

# Verificación oficial de que la renovación automática puede funcionar.
certbot renew --dry-run

echo "HTTPS + balanceo configurados para $DOMAIN"
