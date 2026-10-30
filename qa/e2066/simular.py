"""Reproducción determinista de escenarios sintéticos para estudiar spans.

IMPORTANTE: esto NO representa operaciones wallet ni resultados económicos reales.
Se asume disponibilidad fija al instante de decisión y aceptación de todos los
bids elegibles; no se modelan retrasos de ACK, competencia ni colas.
"""
from __future__ import annotations

import argparse
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
import json
from pathlib import Path

from .comun import SPANS, csv_string, write_atomic

CENT = Decimal("0.01")
FIELDS = (
    "evidenceType", "scenario", "selectionPolicy", "spanSeconds", "canBid",
    "reason", "offersEligible", "deficitKwh", "boughtKwh", "coverageRate",
    "marketPurchaseCost", "estimatedSettlementCost", "estimatedTotalCost",
    "baselineCost", "estimatedSavings", "estimatedSavingsRate", "selectedOffers",
)


def _d(value) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("Decimal inválido")
    try:
        val = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"Número inválido: {value!r}") from exc
    if not val.is_finite():
        raise ValueError("Número no finito")
    return val


def _cent(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _transport(offer: dict, penalty) -> tuple[Decimal, Decimal]:
    distance = _d(offer["distanceMeters"])
    nominal = _d(offer["pricePerEnergy"])
    if distance < 0 or nominal < 0:
        raise ValueError("Distancia/precio negativo")
    rate = _d(offer["transportCost"]) if offer["routeEnabled"] else _d(penalty)
    if rate < 0:
        raise ValueError("Tarifa transporte negativa")
    per = _cent(distance / Decimal("1000") * rate)
    return distance, _cent(nominal + per)


def simulate_one(scenario: dict, *, span: int, policy: str) -> dict:
    """Snapshot al comenzar la compra: reglas E2-036/037 y wallet simulada."""
    if span not in SPANS:
        raise ValueError("Span fuera del conjunto justificado")
    if policy not in {"CHEAPEST_EFFECTIVE_PRICE", "MIN_DISTANCE"}:
        raise ValueError("Política de selección desconocida")
    deficit = int(scenario["deficitKwh"])
    min_bid = int(scenario["minBid"])
    window = int(scenario["lastBidSeconds"])
    closes = int(scenario["marketClosesSeconds"])
    if deficit < 0 or min_bid < 1 or window <= 0 or closes < window:
        raise ValueError("Escenario con restricciones del mercado inválidas")
    budget = _d(scenario["walletCapacityCr"])
    rate = _d(scenario["imbalancePrice"])
    if rate < 0:
        raise ValueError("Tarifa de desbalance negativa")
    baseline = _cent(Decimal(deficit) * rate)
    permitted = span < window and span < closes
    entries = []
    if permitted:
        for offer in scenario["offers"]:
            if offer.get("state", "ACTIVE") != "ACTIVE":
                continue
            if offer.get("uncertain", False) or offer.get("stale", False):
                continue
            arrive = int(offer["arriveAtSeconds"])
            expires = int(offer["expiresAtSeconds"])
            if not (arrive <= span < expires):
                continue
            remaining = int(offer["remainingKwh"])
            if remaining < min_bid:
                continue
            distance, price = _transport(offer, scenario["disabledRouteTransportCost"])
            entries.append((offer, distance, price))
    if policy == "CHEAPEST_EFFECTIVE_PRICE":
        entries.sort(key=lambda e: (e[2], e[1], -int(e[0]["remainingKwh"]), e[0]["cityId"], e[0]["offerId"]))
    else:
        entries.sort(key=lambda e: (e[1], e[2], -int(e[0]["remainingKwh"]), e[0]["cityId"], e[0]["offerId"]))
    covered, purchase, used = 0, Decimal("0"), []
    capacity = max(Decimal("0"), budget)
    if permitted:
        for offer, _, price in entries:
            needed = deficit - covered
            if needed < min_bid:
                break
            quantity = min(needed, int(offer["remainingKwh"]))
            # Cada kWh paga precio efectivo redondeado según E2-036.
            if price > 0:
                quantity = min(quantity, int((capacity / price).to_integral_value(rounding=ROUND_DOWN)))
            if quantity < min_bid:
                continue
            amount = Decimal(quantity) * price
            purchase += amount
            capacity -= amount
            covered += quantity
            used.append(f"{offer['offerId']}:{quantity}")
    predicted_settlement = _cent(Decimal(max(0, deficit - covered)) * rate)
    estimated = _cent(purchase + predicted_settlement)
    savings = _cent(baseline - estimated)
    return {
        "evidenceType": "SYNTHETIC_COUNTERFACTUAL_NOT_REAL_RF06",
        "scenario": scenario["id"], "selectionPolicy": policy, "spanSeconds": span,
        "canBid": permitted, "reason": "" if permitted else "LAST_BID_PASSED",
        "offersEligible": len(entries), "deficitKwh": deficit, "boughtKwh": covered,
        "coverageRate": str(_d(covered) / _d(deficit)) if deficit else "1",
        "marketPurchaseCost": str(_cent(purchase)),
        "estimatedSettlementCost": str(predicted_settlement),
        "estimatedTotalCost": str(estimated), "baselineCost": str(baseline),
        "estimatedSavings": str(savings),
        "estimatedSavingsRate": str(savings / baseline) if baseline else "",
        "selectedOffers": ";".join(used),
    }


def simulate_scenarios(document: dict) -> list[dict]:
    if document.get("kind") != "SYNTHETIC_EXPERIMENT_FIXTURES":
        raise ValueError("Solo se aceptan fixtures identificadas como sintéticas")
    scenarios = document.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise ValueError("Se requiere un arreglo no vacío de escenarios")
    if len({s["id"] for s in scenarios}) != len(scenarios):
        raise ValueError("IDs de escenarios duplicados")
    return [simulate_one(s, span=span, policy=policy) for s in scenarios
            for policy in ("CHEAPEST_EFFECTIVE_PRICE", "MIN_DISTANCE") for span in SPANS]


def main(argv=None):
    parser = argparse.ArgumentParser(description="E2-066: comparar spans en mercados sintéticos idénticos")
    parser.add_argument("--scenarios", default=str(Path(__file__).with_name("escenarios_sinteticos.json")))
    parser.add_argument("--out-dir", default="datos_e2066/simulacion")
    args = parser.parse_args(argv)
    document = json.loads(Path(args.scenarios).read_text(encoding="utf-8"))
    results = simulate_scenarios(document)
    out = Path(args.out_dir)
    write_atomic(out / "escenarios_resultados.csv", csv_string(results, FIELDS))
    lines = [
        "# E2-066 — Resultados de simulación sintética (NO datos reales)", "",
        "**No son métricas E2-065**: el simulador no se conecta a Wallet, ni envía bids ni promete aceptación.",
        "Se mantienen fijos ofertas, horarios, rutas y presupuesto *dentro de cada escenario*; cambia solo el span.",
        "El cálculo supone que las ofertas disponibles al decidir son aceptadas inmediatamente, sin competencia ni latencia.", "",
        f"Escenarios: {len(document['scenarios'])}. Políticas: 2. Spans: {len(SPANS)}. Ejecuciones: {len(results)}.", "",
        "| Escenario | Política | Span (s) | Compra simulada (kWh) | Costo total estimado (CR) | Ahorro estimado (CR) |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for row in results:
        lines.append(f"| {row['scenario']} | {row['selectionPolicy']} | {row['spanSeconds']} | "
                     f"{row['boughtKwh']} | {row['estimatedTotalCost']} | {row['estimatedSavings']} |")
    lines.extend(["", "No confundir estos escenarios sintéticos con evidencia de mercado real ni conclusiones causales de producción."])
    write_atomic(out / "informe_simulacion.md", "\n".join(lines) + "\n")
    print(f"Simulaciones sintéticas: {len(results)} filas -> {out}")
    print("NO son resultados reales de RF06; contrastar con E2-065 después de ejecutar ciclos.")


if __name__ == "__main__":
    main()
