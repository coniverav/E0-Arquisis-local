#!/usr/bin/env bash
# E2-064: pruebas aisladas en PostgreSQL mediante Docker Compose.
set -euo pipefail
cd "$(dirname "$0")"

DB_NAME='energyshark_test_e2064'

echo 'Construyendo imagen de pruebas...'
docker compose build migrate
docker compose up -d db

for i in $(seq 1 40); do
  if docker compose exec -T db sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"' >/dev/null 2>&1; then
    break
  fi
  if [[ "$i" -eq 40 ]]; then
    echo 'ERROR: PostgreSQL no está listo' >&2
    exit 1
  fi
  sleep 1
done

# Nunca tocar la base principal. Obtener usuario y password del contenedor db.
echo "Preparando base de pruebas ${DB_NAME} (se elimina si ya existe)..."
docker compose exec -T db sh -c \
  'PGPASSWORD="$POSTGRES_PASSWORD" psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "DROP DATABASE IF EXISTS energyshark_test_e2064 WITH (FORCE)"'
docker compose exec -T db sh -c \
  'PGPASSWORD="$POSTGRES_PASSWORD" psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "CREATE DATABASE energyshark_test_e2064"'

DATABASE_URL_TEST="$(docker compose config --format json | python3 -c '
import json, sys
from urllib.parse import urlsplit, urlunsplit
c = json.load(sys.stdin)
u = c["services"]["migrate"]["environment"]["DATABASE_URL"]
p = urlsplit(u)
if p.hostname != "db":
    raise SystemExit("La URL de migrate debe usar host db")
print(urlunsplit(p._replace(path="/energyshark_test_e2064")))
')"

echo 'Comprobando heads de Alembic...'
HEADS="$(docker compose run --rm --no-deps -e DATABASE_URL="$DATABASE_URL_TEST" migrate alembic heads)"
printf '%s\n' "$HEADS"
HEAD_COUNT="$(printf '%s\n' "$HEADS" | grep -c '(head)' || true)"
if [[ "$HEAD_COUNT" -ne 1 ]]; then
  echo 'ERROR: hay múltiples heads de Alembic. Debes crear una revisión merge antes de probar.' >&2
  exit 1
fi

echo 'Migrando solo la base de pruebas...'
docker compose run --rm --no-deps -e DATABASE_URL="$DATABASE_URL_TEST" migrate alembic upgrade head

TESTS=(
  tests/test_e2064_buyer_policy.py
  tests/test_e2054_autonomous_buy.py
  tests/test_offer_selection.py
  tests/test_transport_pricing.py
  tests/test_buy_bids.py
  tests/test_e2062_strategy_edit.py
  tests/test_strategy_config_ad6.py
)

echo 'Probando E2-064 y dependencias E2-036/037/038/054/062...'
docker compose run --rm --no-deps -e DATABASE_URL="$DATABASE_URL_TEST" \
  migrate python -m pytest -q "${TESTS[@]}" --tb=short

if [[ "${SOLO_E2064:-0}" == '1' ]]; then
  echo 'Pruebas focalizadas E2-064 completadas.'
  exit 0
fi

echo 'Ejecutando la suite completa...'
docker compose run --rm --no-deps -e DATABASE_URL="$DATABASE_URL_TEST" \
  migrate python -m pytest -q tests --tb=short
