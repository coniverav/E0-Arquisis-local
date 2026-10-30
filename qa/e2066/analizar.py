"""E2-066: comparación observacional RF06; nunca mezcla simulaciones con datos reales.

Fuente: E2-065 /market/cycle-metrics/compare. El objetivo es medir y auditar,
NO atribuir causalidad a cambios entre ciclos de mercados distintos.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from statistics import mean, median

from .comun import csv_string, load_metrics, load_plan, write_atomic

OUTPUT_FIELDS = (
    "slot", "block", "position", "cycleId", "plannedSpanSeconds", "actualSpanSeconds",
    "strategyVersion", "plannedSelectionPolicy", "actualSelectionPolicy", "actualAcceptancePolicy",
    "status", "baselineSource", "deficitKwh", "boughtKwh", "coveredKwh",
    "coverageRate", "rejectedBids", "buyCost", "transportCost", "settlementCost",
    "realCost", "baselineCost", "savingsVsBaseline", "savingsRate", "costPerDeficitKwh",
    "observedRegime", "offersObserved", "liquidityRatio", "disabledRoutesShare",
    "marketWindowSeconds", "eligible", "exclusionReason",
)
SUMMARY_FIELDS = (
    "spanSeconds", "planned", "eligible", "excluded", "meanCoverageRate",
    "medianCoverageRate", "meanSavingsRate", "medianSavingsRate",
    "meanRealCostPerDeficitKwh", "meanRejectedBids",
)
REGIME_FIELDS = ("observedRegime", "spanSeconds", "eligible", "meanCoverageRate", "meanSavingsRate", "meanRejectedBids")
BLOCK_FIELDS = ("block", "spanSeconds", "baselineSlot", "comparisonSlot", "savingsRateDifferenceVsSpan0", "coverageDifferenceVsSpan0")


def _decimal(value) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return parsed if parsed.is_finite() else None


def analyze(plan: list[dict[str, str]], metrics: dict[str, dict]) -> tuple[list[dict], list[dict], list[dict]]:
    rows = []
    for slot in plan:
        cid = slot["cycleId"].strip()
        item = metrics.get(cid) if cid else None
        record = {"slot": slot["slot"], "block": slot["block"], "position": slot["position"],
                  "cycleId": cid, "plannedSpanSeconds": slot["spanSeconds"],
                  "plannedSelectionPolicy": slot["selectionPolicy"],
                  "observedRegime": slot["observedRegime"], "offersObserved": slot["offersObserved"],
                  "liquidityRatio": slot["liquidityRatio"], "disabledRoutesShare": slot["disabledRoutesShare"],
                  "marketWindowSeconds": slot["marketWindowSeconds"], "actualSpanSeconds": "",
                  "strategyVersion": "", "actualSelectionPolicy": "", "actualAcceptancePolicy": "", "status": "",
                  "baselineSource": "", "deficitKwh": "", "boughtKwh": "", "coveredKwh": "",
                  "coverageRate": "", "rejectedBids": "", "buyCost": "", "transportCost": "",
                  "settlementCost": "", "realCost": "", "baselineCost": "", "savingsVsBaseline": "",
                  "savingsRate": "", "costPerDeficitKwh": "", "eligible": "NO", "exclusionReason": ""}
        if item is not None:
            for key in ("strategyVersion", "status", "baselineSource", "deficitKwh", "boughtKwh", "coveredKwh",
                        "coverageRate", "rejectedBids", "buyCost", "transportCost", "settlementCost",
                        "realCost", "baselineCost", "savingsVsBaseline"):
                record[key] = "" if item.get(key) is None else str(item[key])
            record["actualSpanSeconds"] = str(item.get("spanSeconds", ""))
            record["actualSelectionPolicy"] = str(item.get("selectionPolicy", ""))
            record["actualAcceptancePolicy"] = str(item.get("acceptancePolicy", ""))
        if not cid:
            reason = "SLOT_WITHOUT_CYCLE_ID"
        elif item is None:
            reason = "METRICS_NOT_FOUND"
        elif str(item.get("spanSeconds")) != slot["spanSeconds"]:
            reason = "SPAN_MISMATCH_AD6"
        elif item.get("selectionPolicy") != slot["selectionPolicy"]:
            reason = "SELECTION_POLICY_MISMATCH"
        elif item.get("acceptancePolicy") != slot["acceptancePolicy"]:
            reason = "ACCEPTANCE_POLICY_MISMATCH"
        elif slot["marketWindowSeconds"] and int(slot["spanSeconds"]) >= int(slot["marketWindowSeconds"]):
            reason = "SPAN_EXCEEDS_OBSERVED_BID_WINDOW"
        elif item.get("status") != "FINAL":
            reason = "NOT_FINAL"
        elif item.get("baselineSource") != "SETTLEMENT":
            reason = "BASELINE_NOT_CONFIRMED"
        elif item.get("missingTransportTrades", 0) != 0:
            reason = "MISSING_TRANSPORT"
        elif _decimal(item.get("deficitKwh")) is None or _decimal(item.get("deficitKwh")) <= 0:
            reason = "ZERO_OR_UNKNOWN_DEFICIT"
        elif _decimal(item.get("baselineCost")) is None or _decimal(item.get("realCost")) is None:
            reason = "UNKNOWN_COST"
        elif _decimal(item.get("baselineCost")) <= 0:
            reason = "ZERO_BASELINE"
        else:
            reason = ""
        if reason:
            record["exclusionReason"] = reason
        else:
            b, c = _decimal(item["baselineCost"]), _decimal(item["realCost"])
            d = _decimal(item["deficitKwh"])
            record["savingsRate"] = str((b - c) / b)
            record["costPerDeficitKwh"] = str(c / d)
            record["eligible"] = "YES"
        rows.append(record)

    byspan = defaultdict(list)
    for row in rows:
        byspan[int(row["plannedSpanSeconds"])].append(row)
    summaries = []
    for span in sorted(byspan):
        records = byspan[span]
        eligible = [r for r in records if r["eligible"] == "YES"]

        def avg(key):
            vals = [float(r[key]) for r in eligible if r[key] != ""]
            return f"{mean(vals):.6f}" if vals else ""

        def med(key):
            vals = [float(r[key]) for r in eligible if r[key] != ""]
            return f"{median(vals):.6f}" if vals else ""

        summaries.append({
            "spanSeconds": str(span), "planned": str(len(records)),
            "eligible": str(len(eligible)), "excluded": str(len(records) - len(eligible)),
            "meanCoverageRate": avg("coverageRate"), "medianCoverageRate": med("coverageRate"),
            "meanSavingsRate": avg("savingsRate"), "medianSavingsRate": med("savingsRate"),
            "meanRealCostPerDeficitKwh": avg("costPerDeficitKwh"),
            "meanRejectedBids": avg("rejectedBids"),
        })

    block_rows = []
    blocks = sorted({int(r["block"]) for r in rows})
    for block in blocks:
        group = [r for r in rows if int(r["block"]) == block and r["eligible"] == "YES"]
        reference = next((r for r in group if int(r["plannedSpanSeconds"]) == 0), None)
        if reference is None:
            continue
        for item in group:
            if int(item["plannedSpanSeconds"]) == 0:
                continue
            delta_savings = Decimal(item["savingsRate"]) - Decimal(reference["savingsRate"])
            delta_coverage = Decimal(item["coverageRate"]) - Decimal(reference["coverageRate"])
            block_rows.append({"block": str(block), "spanSeconds": item["plannedSpanSeconds"],
                               "baselineSlot": reference["slot"], "comparisonSlot": item["slot"],
                               "savingsRateDifferenceVsSpan0": str(delta_savings),
                               "coverageDifferenceVsSpan0": str(delta_coverage)})
    return rows, summaries, block_rows


def summarize_regimes(rows: list[dict]) -> list[dict]:
    """Agrupa ex post por régimen observado; evidencia descriptiva, no causal."""
    groups = defaultdict(list)
    for row in rows:
        if row["eligible"] == "YES" and row["observedRegime"]:
            groups[(row["observedRegime"], int(row["plannedSpanSeconds"]))].append(row)
    result = []
    for (regime, span), records in sorted(groups.items()):
        result.append({
            "observedRegime": regime, "spanSeconds": str(span),
            "eligible": str(len(records)),
            "meanCoverageRate": f"{mean(float(r['coverageRate']) for r in records):.6f}",
            "meanSavingsRate": f"{mean(float(r['savingsRate']) for r in records):.6f}",
            "meanRejectedBids": f"{mean(float(r['rejectedBids']) for r in records):.6f}",
        })
    return result


def report_markdown(rows: list[dict], summaries: list[dict], blocks: list[dict]) -> str:
    included = [r for r in rows if r["eligible"] == "YES"]
    reasons = defaultdict(int)
    for row in rows:
        if row["exclusionReason"]:
            reasons[row["exclusionReason"]] += 1
    text = [
        "# E2-066 — Comparación observacional de spans (RF06)", "",
        "**Naturaleza:** resultados de ciclos REALES, derivados del endpoint E2-065; no son resultados simulados.",
        f"**Ciclos planificados:** {len(rows)}. **Elegibles para comparación económica:** {len(included)}.",
        "", "## Promedios descriptivos por span", "",
        "| Span (s) | Finales válidos | Cobertura media | Ahorro relativo medio | Costo real / kWh déficit | Rechazos medios |",
        "|---:|---:|---:|---:|---:|---:|",
    ]
    for s in summaries:
        text.append(f"| {s['spanSeconds']} | {s['eligible']} / {s['planned']} | "
                    f"{s['meanCoverageRate'] or '—'} | {s['meanSavingsRate'] or '—'} | "
                    f"{s['meanRealCostPerDeficitKwh'] or '—'} | {s['meanRejectedBids'] or '—'} |")
    text.extend(["", "## Calidad y exclusiones", ""])
    if reasons:
        text.extend(f"- `{k}`: {v} ciclo(s)." for k, v in sorted(reasons.items()))
    else:
        text.append("- Sin exclusiones.")
    text.extend(["", f"Comparaciones descriptivas dentro de bloques con span=0 válido: {len(blocks)}.", "",
                 "## Interpretación y control de sesgos", "",
                 "- **No atribuir causalidad a diferencias observadas:** cada ciclo enfrenta un mercado distinto.",
                 "- Conservar fijos `selectionPolicy`, `acceptancePolicy`, markup y fracción de venta; registrar variaciones exógenas.",
                 "- Anotar ofertas, liquidez, fracción de rutas deshabilitadas, horario, precios y ventana `lastBidAt`.",
                 "- El promedio se calcula solo con `FINAL` y `baselineSource=SETTLEMENT`. Ciclos sin déficit se reportan, no se usan en el cociente.",
                 "- Un `span` mayor o igual a la ventana de bids se excluye, no se confunde con fracaso comercial.",
                 "- Los contrastes por bloque son **descriptivos**, no equivalen a un experimento pareado real.",
                 "- No presentar significancia estadística sin tamaño muestral y diseño suficientes.",
                 "- Para comparar bajo condiciones *idénticas*, usar el simulador controlado por escenarios, señalado como simulación.",
                 "", "## Próximos pasos", "",
                 "1. Recolectar más ciclos `FINAL` si faltan repeticiones o hay desbalance entre spans.",
                 "2. Separar resultados por régimen de mercado y revisar la distribución de variables de control.",
                 "3. Revisar eventos pendientes antes de concluir que un span mejora el costo o la cobertura.", ""])
    return "\n".join(text)


def main(argv=None):
    parser = argparse.ArgumentParser(description="E2-066: análisis RF06 por ciclo real")
    parser.add_argument("--plan", default="datos_e2066/plan_spans.csv")
    parser.add_argument("--metrics", default="datos_e2066/metricas_recogidas.json")
    parser.add_argument("--out-dir", default="datos_e2066/resultados")
    args = parser.parse_args(argv)
    from pathlib import Path
    plan, metrics = load_plan(args.plan), load_metrics(args.metrics)
    rows, summaries, blocks = analyze(plan, metrics)
    regimes = summarize_regimes(rows)
    out = Path(args.out_dir)
    for name, payload in (
        ("comparacion_por_ciclo.csv", csv_string(rows, OUTPUT_FIELDS)),
        ("resumen_por_span.csv", csv_string(summaries, SUMMARY_FIELDS)),
        ("contrastes_por_bloque.csv", csv_string(blocks, BLOCK_FIELDS)),
        ("resumen_por_regimen.csv", csv_string(regimes, REGIME_FIELDS)),
        ("informe_RF06.md", report_markdown(rows, summaries, blocks)),
    ):
        write_atomic(out / name, payload)
    print(f"Análisis guardado en {out}. Ciclos finales válidos: {sum(r['eligible'] == 'YES' for r in rows)} / {len(rows)}.")
    print("Importante: resultados observacionales; no inferir causalidad sin controlar cambios del mercado.")


if __name__ == "__main__":
    main()
