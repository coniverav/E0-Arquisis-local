"""Obtiene todas las páginas de E2-065 sin mutar datos ni realizar operaciones de mercado."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .comun import write_atomic


def collect(fetch_page, *, status: str | None = None, limit: int = 200, max_pages: int = 1000) -> dict:
    """fetch_page(params) -> dict; testable sin red."""
    if status not in (None, "FINAL", "IN_PROGRESS", "AWAITING_SETTLEMENT", "AWAITING_TRANSPORT"):
        raise ValueError("status desconocido")
    if not 1 <= limit <= 200 or max_pages < 1:
        raise ValueError("Limit o max_pages inválidos")
    result, offset, total = [], 0, None
    while True:
        if len(result) // limit >= max_pages:
            raise RuntimeError("Exceso de páginas: abortar antes de truncar silenciosamente")
        params = {"offset": offset, "limit": limit}
        if status:
            params["status"] = status
        page = fetch_page(params)
        if not isinstance(page, dict) or not isinstance(page.get("items"), list) or type(page.get("total")) is not int:
            raise ValueError("Respuesta /cycle-metrics/compare inválida")
        if total is None:
            total = page["total"]
        elif page["total"] != total:
            raise RuntimeError("El total cambió durante paginación; repetir para snapshot consistente")
        items = page["items"]
        if len(items) > limit:
            raise ValueError("Servidor devolvió más elementos que el límite")
        result.extend(items)
        offset += len(items)
        if offset >= total:
            break
        if not items:
            raise RuntimeError("Página vacía antes de recopilar todas las métricas")
    ids = [m.get("cycleId") for m in result]
    if len(ids) != len(set(ids)):
        raise RuntimeError("La paginación devolvió cycleIds repetidos; repetir recolección")
    return {"statusFilter": status, "total": total, "items": result,
            "collectedAt": datetime.now(timezone.utc).isoformat()}


def main(argv=None):
    parser = argparse.ArgumentParser(description="E2-066: descargar métricas E2-065 de modo lectura")
    parser.add_argument("--base-url", default="http://localhost:8001")
    parser.add_argument("--output", default="datos_e2066/metricas_recogidas.json")
    parser.add_argument("--status", default="ALL", choices=["ALL", "FINAL", "IN_PROGRESS", "AWAITING_SETTLEMENT", "AWAITING_TRANSPORT"])
    parser.add_argument("--token-env", default="RF06_JWT")
    parser.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args(argv)
    if not args.base_url.startswith(("http://", "https://")):
        parser.error("URL inválida")
    token = os.environ.get(args.token_env)

    def fetch(params):
        url = args.base_url.rstrip("/") + "/market/cycle-metrics/compare?" + urlencode(params)
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        try:
            with urlopen(Request(url, headers=headers), timeout=args.timeout) as response:
                return json.load(response)
        except HTTPError as exc:
            raise RuntimeError(f"HTTP {exc.code} consultando métricas") from exc
        except URLError as exc:
            raise RuntimeError(f"No fue posible consultar métricas: {exc.reason}") from exc

    data = collect(fetch, status=None if args.status == "ALL" else args.status)
    data["source"] = args.base_url.rstrip("/") + "/market/cycle-metrics/compare"
    write_atomic(args.output, json.dumps(data, ensure_ascii=False, indent=2))
    print(f"Recogidos {data['total']} ciclos (status={args.status}). Archivo: {args.output}")
    print("El archivo solo contiene métricas E2-065; asociar los cycleId a los slots del plan.")


if __name__ == "__main__":
    main()
