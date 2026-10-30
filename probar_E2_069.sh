#!/usr/bin/env bash
# E2-069 AD4: concurrencia 40 ms con PostgreSQL real y Wallet FAKE.
# No depende de E2-067/E2-068 ni requiere conectarse al servidor central.
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -f docker-compose.yml || ! -f master/tests/test_bids_concurrentes.py ]]; then
  echo 'ERROR: instala el paquete primero (python3 aplicar_e2_069.py).' >&2
  exit 1
fi
if ! command -v docker >/dev/null 2>&1; then
  echo 'ERROR: Docker no disponible en PATH.' >&2
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo 'ERROR: python3 no disponible en PATH.' >&2
  exit 1
fi

ROUNDS="${E2069_ROUNDS:-4}"
if ! [[ "$ROUNDS" =~ ^([1-9]|1[0-9]|20)$ ]]; then
  echo 'ERROR: E2069_ROUNDS debe ser un entero entre 1 y 20.' >&2
  exit 2
fi

MODE="${MODO:-focal}"
case "$MODE" in
  focal)
    TESTS=(tests/test_bids_concurrentes.py)
    ;;
  relacionados)
    TESTS=(
      tests/test_bids_concurrentes.py
      tests/test_seller_bid_acceptance.py
      tests/test_seller_bid_rejections.py
      tests/test_incoming_bids.py
      tests/test_e2053_offer_remaining.py
    )
    # Si el script independiente de renombrado se usó, adaptar nombres.
    for i in "${!TESTS[@]}"; do
      if [[ "${TESTS[$i]}" == 'tests/test_e2053_offer_remaining.py' &&
            ! -f 'master/tests/test_e2053_offer_remaining.py' &&
            -f 'master/tests/test_offer_remaining.py' ]]; then
        TESTS[$i]='tests/test_offer_remaining.py'
      fi
    done
    ;;
  *)
    echo 'ERROR: MODO debe ser focal o relacionados.' >&2
    exit 2
    ;;
esac
for test_file in "${TESTS[@]}"; do
  [[ -f "master/$test_file" ]] || { echo "ERROR: falta master/$test_file" >&2; exit 1; }
done

# Garantiza que el reporte de esta corrida no arrastre resultados anteriores.
mkdir -p datos_e2069
find datos_e2069 -maxdepth 1 -type f -name '*.json' -delete
rm -f datos_e2069/RESUMEN_E2_069.md

printf '%s\n' 'E2-069: prueba AD4 con PostgreSQL real y Wallet simulada.' \
  "Modo: $MODE | rondas de ráfaga: $ROUNDS | sin conexión a Wallet Central."

docker compose up -d db
docker compose build migrate

# Evita SQL dinámico sin comillas y la regresión de E2-068.
docker compose exec -T db sh -ec '
 dbname=energyshark_test_e2069
 if ! PGPASSWORD="$POSTGRES_PASSWORD" psql -U "$POSTGRES_USER" -d postgres -Atc "SELECT datname FROM pg_database" | grep -Fxq "$dbname"; then
   PGPASSWORD="$POSTGRES_PASSWORD" createdb -U "$POSTGRES_USER" "$dbname"
 fi
'

TEST_URL="$(docker compose config --format json | python3 -c '
import json,sys
from urllib.parse import urlsplit,urlunsplit
config=json.load(sys.stdin)
url=config["services"]["migrate"]["environment"]["DATABASE_URL"]
parts=urlsplit(url)
if parts.hostname != "db":
    raise SystemExit("ERROR: DATABASE_URL de migrate debe apuntar a db")
print(urlunsplit(parts._replace(path="/energyshark_test_e2069")))
')"

# El código de master/tests está incluido en la imagen migrate.
# No se instala nada adicional con pip ni se usa el connector.
docker compose run --rm --no-deps \
  -v "$PWD/datos_e2069:/e2069_reports" \
  -e DATABASE_URL="$TEST_URL" \
  -e E2069_REPORT_DIR=/e2069_reports \
  -e E2069_ROUNDS="$ROUNDS" \
  migrate python -m pytest -q -s --tb=short "${TESTS[@]}"

python3 -m qa.e2069.resumir --directory datos_e2069
printf '\n%s\n' 'E2-069: pruebas completas. Revisa datos_e2069/RESUMEN_E2_069.md.'
