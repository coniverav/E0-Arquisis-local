#!/usr/bin/env bash
# E2-088/E2-089: reproducibles y 100% locales. Nunca envia mensajes al broker oficial.
set -euo pipefail
cd "$(dirname "$0")"

if [[ "$PWD" == /opt/energyshark* ]]; then
  echo "ERROR: esta prueba no se ejecuta en el servidor productivo." >&2
  exit 2
fi
for path in master/tests/test_e2046_dedup_replay.py master/tests/test_e2088_e2089_audit_demo.py connector/tests/test_city_identity.py; do
  [[ -f "$path" ]] || { echo "Falta $path; aplicar primero los archivos QA." >&2; exit 2; }
done
command -v docker >/dev/null || { echo "Docker no disponible." >&2; exit 2; }

autogen="e2088_089_$$_$RANDOM"
NETWORK="$autogen"
DB_CONTAINER="${autogen}_db"
TEST_DB="energyshark_test_e2088_089"
TEST_USER="qa_e2"
TEST_PASSWORD="qa_local_only"
IMAGE="energyshark-e2088-e2089:qa-local"
REPORT_DIR="$PWD/datos_e2088_089"
mkdir -p "$REPORT_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

cleanup() {
  docker rm -f "$DB_CONTAINER" >/dev/null 2>&1 || true
  docker network rm "$NETWORK" >/dev/null 2>&1 || true
}
trap cleanup EXIT

printf 'E2-088/089: entorno aislado, Wallet simulada y broker en memoria.\n'
printf 'No se utiliza .env, RabbitMQ real ni base de produccion.\n'

docker build -f qa/e2088_089/Dockerfile.qa \
  -t "$IMAGE" ./master

# Fallar antes del test si Docker genero codigo vacio como en la build original.
local_hash="$(sha256sum master/app/services/wallet_observability.py | cut -d' ' -f1)"
image_hash="$(docker run --rm --entrypoint sha256sum "$IMAGE" /app/app/services/wallet_observability.py | cut -d' ' -f1)"
if [[ "$local_hash" != "$image_hash" ]]; then
  echo "ERROR: contenido de imagen distinto del repositorio, no ejecutar." >&2
  exit 1
fi

docker network create --internal "$NETWORK" >/dev/null
docker run -d --rm --name "$DB_CONTAINER" \
  --network "$NETWORK" --network-alias db \
  -e "POSTGRES_DB=$TEST_DB" \
  -e "POSTGRES_USER=$TEST_USER" \
  -e "POSTGRES_PASSWORD=$TEST_PASSWORD" \
  postgres:16-alpine >/dev/null

ready=0
for _ in $(seq 1 45); do
  if docker exec "$DB_CONTAINER" pg_isready -U "$TEST_USER" -d "$TEST_DB" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done
if [[ "$ready" -ne 1 ]]; then
  echo "ERROR: PostgreSQL QA no inicio correctamente." >&2
  exit 1
fi

printf '\n=== E2-088: replay de idpk y auditoria; E2-089: API de auditoria ===\n'
docker run --rm --network "$NETWORK" \
  -e "DATABASE_URL=postgresql+psycopg://$TEST_USER:$TEST_PASSWORD@db:5432/$TEST_DB" \
  -e CITY_ID=KLD -e WALLET_BASE_URL=https://wallet.test \
  -v "$PWD/connector:/connector:ro" \
  -v "$PWD/master/app:/app/app:ro" \
  -v "$PWD/master/tests:/app/tests:ro" \
  -v "$PWD/master/alembic:/app/alembic:ro" \
  "$IMAGE" python -m pytest -vv -s --tb=short \
    tests/test_e2046_dedup_replay.py::test_seller_bid_and_wallet_replays_do_not_repeat_economic_effects \
    tests/test_e2088_e2089_audit_demo.py \
    tests/test_e2075_audit_v3.py::test_anomalies_keep_frontend_shape_and_filter_identity_mismatch \
    tests/test_e2075_audit_v3.py::test_intercity_filters_correlation_and_pagination_without_payload \
  2>&1 | tee "$REPORT_DIR/${STAMP}_backend.txt"

printf '\n=== E2-089: connector real con broker simulado; NACK/DISCARD ===\n'
docker run --rm --network none \
  -e RABBITMQ_URL=amqps://unused.invalid/qa \
  -e RABBITMQ_QUEUE=city.KLD.q -e CITY_ID=KLD \
  -e RABBITMQ_OUTBOUND_EXCHANGE=energy.x \
  -e RABBITMQ_CENTRAL_ROUTING_KEY=central \
  -v "$PWD/connector:/connector:ro" \
  -w /connector -e PYTHONPATH=/connector \
  "$IMAGE" python -m pytest -vv -s --tb=short \
    tests/test_city_identity.py \
    tests/test_directed_ack_nack.py \
    tests/test_connector_audit.py \
  2>&1 | tee "$REPORT_DIR/${STAMP}_connector.txt"

printf '\nE2-088/089: pruebas aprobadas. Evidencia local en %s\n' "$REPORT_DIR"
printf 'ATENCION: esta corrida no demuestra que una anomalia real ocurrio en el broker de la central.\n'
