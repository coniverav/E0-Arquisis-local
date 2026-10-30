"""Prepara una versión AD6 PENDING conservando otros parámetros.

Solo envía POST si el usuario pasa --apply. Nunca toca un ciclo ya abierto.
El JWT viene de variable de entorno y nunca se imprime.
"""
from __future__ import annotations

import argparse
import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .comun import load_plan


def pending_payload(snapshot: dict, slot: dict) -> dict:
    """Construye una edición versionada basada en la última configuración."""
    versions = [v for v in (snapshot.get("active"), snapshot.get("pending")) if v]
    if not versions:
        raise ValueError("Sin estrategia activa/pendiente: no generar defaults silenciosos")
    latest = max(versions, key=lambda v: v["version"])
    required = ("offerPriceMarkup", "offerQuantityFraction", "bidAcceptancePolicy",
                "offerSelectionPolicy", "spanSeconds", "version")
    if any(k not in latest for k in required):
        raise ValueError("Faltan campos AD6 en GET /market/strategy")
    payload = {key: latest[key] for key in required if key != "version"}
    payload.update(
        spanSeconds=int(slot["spanSeconds"]),
        offerSelectionPolicy=slot["selectionPolicy"],
        bidAcceptancePolicy=slot["acceptancePolicy"],
        expectedVersion=latest["version"],
    )
    return payload


def _json_request(base: str, path: str, token: str | None, *, payload: dict | None = None,
                  timeout: float = 15.0) -> dict:
    if not base.startswith(("http://", "https://")):
        raise ValueError("--base-url debe comenzar con http:// o https://")
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if payload is None:
        method, body = "GET", None
    else:
        method, body = "POST", json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = Request(base.rstrip("/") + path, headers=headers, data=body, method=method)
    try:
        with urlopen(req, timeout=timeout) as response:
            return json.load(response)
    except HTTPError as exc:
        detail = exc.read(600).decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} en {path}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Error de red en {path}: {exc.reason}") from exc


def main(argv=None):
    parser = argparse.ArgumentParser(description="E2-066: preparar estrategia PENDING para próximo ciclo")
    parser.add_argument("--plan", default="datos_e2066/plan_spans.csv")
    parser.add_argument("--slot", required=True, type=int)
    parser.add_argument("--base-url", default="http://localhost:8001")
    parser.add_argument("--jwt-env", default="RF06_JWT",
                        help="Nombre de la variable que contiene el JWT; nunca se imprime")
    parser.add_argument("--apply", action="store_true",
                        help="Autoriza crear nueva versión PENDING mediante POST")
    args = parser.parse_args(argv)
    slot = next((s for s in load_plan(args.plan) if int(s["slot"]) == args.slot), None)
    if slot is None:
        parser.error("No existe el slot solicitado")
    token = os.environ.get(args.jwt_env)
    snapshot = _json_request(args.base_url, "/market/strategy", token)
    payload = pending_payload(snapshot, slot)
    if args.apply and not token:
        parser.error(f"--apply exige la variable de entorno {args.jwt_env}")
    if args.apply:
        result = _json_request(args.base_url, "/market/strategy", token, payload=payload)
        if result.get("status") != "PENDING" or result.get("spanSeconds") != payload["spanSeconds"]:
            raise RuntimeError("Respuesta de actualización AD6 inesperada")
        print(f"Versión AD6 #{result.get('version')} PENDING para próximo ciclo, span={payload['spanSeconds']}s")
    else:
        print("SOLO VISTA PREVIA: NO se escribió ninguna configuración.")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        print("Para guardar la nueva versión PENDING, repetir con --apply y JWT de operador.")
    print("IMPORTANTE: registrar qué cycleId recibió efectivamente esta versión después de abrir el siguiente ciclo.")


if __name__ == "__main__":
    main()
