#!/usr/bin/env bash
# QA E2-067 local; no red externa, no Wallet, sin Docker/PostgreSQL.
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -f qa/e2067/capturar.py || ! -f qa/e2066/comun.py ]]; then
  echo 'Falta instalar E2-067/E2-066: python3 aplicar_e2_067.py' >&2
  exit 1
fi
PYTHON_BIN="${PYTHON_BIN:-python3}"
"$PYTHON_BIN" -m unittest discover -s qa/e2067/tests -p 'test_*.py' -v
"$PYTHON_BIN" -m qa.e2067.capturar --help >/dev/null
"$PYTHON_BIN" -m qa.e2067.latencias --help >/dev/null
"$PYTHON_BIN" -m qa.e2067.consolidar --help >/dev/null
printf '\nOK: E2-067 QA validado sin ejecutar ni modificar ciclos reales.\n'
