"""Consolida resultados reales RF06 + latencia Wallet con integridad por cycleId.

No convierte métricas pendientes en valores cero; no presenta conclusiones
causales ni mezcla datos simulados con observaciones reales.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path
from statistics import mean, median

from .comun import DATA_DIR, decimal, evidence_path, wallet_path, read_json
from qa.e2066.comun import csv_string, load_plan, write_atomic

CYCLE_FIELDS = (
    'slot', 'block', 'position', 'cycleId', 'environment', 'deploySha',
    'plannedSpanSeconds', 'actualSpanSeconds', 'strategyVersion',
    'plannedSelectionPolicy', 'actualSelectionPolicy',
    'actualAcceptancePolicy', 'cycleStatusConfirmed', 'marketOpenedAt',
    'marketLastBidAt', 'marketClosesAt', 'marketWindowSeconds',
    'earlyCaptureAt', 'finalCaptureAt', 'earlyOffers', 'finalOffers',
    'earlyLiquidityRatio', 'earlyMedianEffectivePrice',
    'earlyDisabledRoutesShare', 'finalMedianEffectivePrice',
    'observedRegime', 'deficitKwh', 'boughtKwh', 'coveredKwh',
    'coverageRate', 'rejectedBids', 'metricStatus', 'baselineSource',
    'realCost', 'baselineCost', 'savingsVsBaseline',
    'eventCount', 'bidCreatedEvents', 'swapSettledEvents',
    'walletAttributed', 'walletExact', 'walletAssumed', 'walletMeanMs',
    'walletP50Ms', 'walletP95Ms', 'walletTimeoutCount', 'wallet429Count',
    'readyForEconomicComparison', 'qualityFlags', 'notes',
)
EVENT_FIELDS = ('slot', 'cycleId', 'at', 'eventType', 'source', 'ref', 'direction',
                'status', 'quantity', 'reason', 'role', 'operationType', 'messageType')
SPAN_FIELDS = ('spanSeconds', 'planned', 'captured', 'final', 'economicallyComparable',
               'meanCoverage', 'medianCoverage', 'meanSavingsRate',
               'withWalletLatency', 'meanWalletP95Ms')


def _string(value) -> str:
    return '' if value is None else str(value)


def build_report(plan: list[dict], data_dir: Path) -> tuple[list[dict], list[dict], list[dict], str]:
    results, event_rows = [], []
    for slot in plan:
        cid = slot['cycleId'].strip()
        early = final = wallet = None
        quality = []
        if cid:
            paths = (evidence_path(data_dir, int(slot['slot']), cid, 'early'),
                     evidence_path(data_dir, int(slot['slot']), cid, 'final'))
            early = read_json(paths[0]) if paths[0].is_file() else None
            final = read_json(paths[1]) if paths[1].is_file() else None
            wp = wallet_path(data_dir, cid)
            wallet = read_json(wp) if wp.is_file() else None
            for phase, item in (('early', early), ('final', final)):
                if item is not None and (item.get('cycleId') != cid or item.get('phase') != phase
                                         or item.get('slot') != int(slot['slot'])
                                         or item.get('source') != 'REAL_API_READONLY'):
                    raise ValueError(f'Evidencia {phase} incompatible con slot {slot["slot"]}')
            if wallet is not None and (wallet.get('cycleId') != cid or
                                       wallet.get('source') != 'REAL_WALLET_TELEMETRY_E2027'):
                raise ValueError('Datos Wallet incompatibles con cycleId real')
        else:
            quality.append('SLOT_NOT_EXECUTED')
        if not early:
            quality.append('EARLY_SNAPSHOT_MISSING')
        if not final:
            quality.append('FINAL_SNAPSHOT_MISSING')
        primary = final or early
        met = (final or {}).get('metrics') or {}
        early_factors = (early or {}).get('externalFactors') or {}
        final_factors = (final or {}).get('externalFactors') or {}
        attributes = (wallet or {}).get('attribution') or {}
        exact = int(attributes.get('DIRECT_CYCLE_ID', 0)) + int(attributes.get('KNOWN_ID_MATCH', 0))
        assumed = int(attributes.get('ASSUMED_ISOLATED', 0))
        if not wallet:
            quality.append('WALLET_LATENCY_NOT_IMPORTED')
        elif assumed:
            quality.append('WALLET_LATENCY_ASSUMED_ISOLATED')
        elif not exact:
            quality.append('WALLET_LATENCY_NOT_CORRELATED')
        if primary:
            quality.extend(primary.get('qualityFlags') or [])
        if met:
            if str(met.get('spanSeconds')) != slot['spanSeconds']:
                quality.append('SPAN_MISMATCH_AD6')
            if met.get('selectionPolicy') != slot['selectionPolicy'] or met.get('acceptancePolicy') != slot['acceptancePolicy']:
                quality.append('POLICY_MISMATCH')
        if not met or met.get('status') != 'FINAL':
            quality.append('METRICS_NOT_FINAL')
        if met and met.get('baselineSource') != 'SETTLEMENT':
            quality.append('BASELINE_NOT_CONFIRMED')
        if met and decimal(met.get('baselineCost')) is None:
            quality.append('BASELINE_UNKNOWN')
        if met and decimal(met.get('realCost')) is None:
            quality.append('REAL_COST_UNKNOWN')
        if met and (decimal(met.get('deficitKwh')) is None or decimal(met.get('deficitKwh')) <= 0):
            quality.append('NO_POSITIVE_DEFICIT')
        if met and decimal(met.get('baselineCost')) is not None and decimal(met.get('baselineCost')) <= 0:
            quality.append('ZERO_BASELINE')
        # Solo una cronología por ciclo. Si falta la captura final,
        # conservar la evidencia temprana sin duplicar los eventos.
        event_source = final or early
        if event_source:
            for e in event_source.get('events', []):
                event_rows.append({'slot': slot['slot'], 'cycleId': cid, **e})
        incompatible = ('SLOT_NOT_EXECUTED', 'FINAL_SNAPSHOT_MISSING',
                        'METRICS_NOT_FINAL', 'SPAN_MISMATCH_AD6', 'POLICY_MISMATCH',
                        'BASELINE_NOT_CONFIRMED', 'BASELINE_UNKNOWN', 'REAL_COST_UNKNOWN',
                        'NO_POSITIVE_DEFICIT', 'ZERO_BASELINE',
                        'UNCONFIRMED_CYCLE_STATUS', 'STRATEGY_VERSION_MISMATCH',
                        'FINAL_CAPTURE_BEFORE_CLOSE')
        comparable = bool(primary and all(flag not in quality for flag in incompatible))
        wallet_stats = (wallet or {}).get('summary') or {}
        events = (final or {}).get('events') or []
        row = {
            'slot': slot['slot'], 'block': slot['block'], 'position': slot['position'],
            'cycleId': cid, 'environment': (primary or {}).get('deployment', {}).get('environment'),
            'deploySha': (primary or {}).get('deployment', {}).get('commit'),
            'plannedSpanSeconds': slot['spanSeconds'],
            'actualSpanSeconds': met.get('spanSeconds'), 'strategyVersion': met.get('strategyVersion'),
            'plannedSelectionPolicy': slot['selectionPolicy'],
            'actualSelectionPolicy': met.get('selectionPolicy'),
            'actualAcceptancePolicy': met.get('acceptancePolicy'),
            'cycleStatusConfirmed': (primary or {}).get('cycle', {}).get('statusConfirmed'),
            'marketOpenedAt': final_factors.get('marketOpensAt') or early_factors.get('marketOpensAt'),
            'marketLastBidAt': final_factors.get('lastBidAt') or early_factors.get('lastBidAt'),
            'marketClosesAt': final_factors.get('closesAt') or early_factors.get('closesAt'),
            'marketWindowSeconds': (final_factors.get('marketWindowSeconds')
                                    or early_factors.get('marketWindowSeconds')),
            'earlyCaptureAt': (early or {}).get('capturedAt'),
            'finalCaptureAt': (final or {}).get('capturedAt'),
            'earlyOffers': early_factors.get('activeOffersInSnapshot'),
            'finalOffers': final_factors.get('activeOffersInSnapshot'),
            'earlyLiquidityRatio': early_factors.get('liquidityRatioAtCapture'),
            'earlyMedianEffectivePrice': early_factors.get('medianEffectivePriceAtCapture'),
            'earlyDisabledRoutesShare': early_factors.get('disabledRoutesShareAtCapture'),
            'finalMedianEffectivePrice': final_factors.get('medianEffectivePriceAtCapture'),
            'observedRegime': slot.get('observedRegime', ''),
            'deficitKwh': met.get('deficitKwh'), 'boughtKwh': met.get('boughtKwh'),
            'coveredKwh': met.get('coveredKwh'), 'coverageRate': met.get('coverageRate'),
            'rejectedBids': met.get('rejectedBids'), 'metricStatus': met.get('status'),
            'baselineSource': met.get('baselineSource'), 'realCost': met.get('realCost'),
            'baselineCost': met.get('baselineCost'), 'savingsVsBaseline': met.get('savingsVsBaseline'),
            # Falta de captura NO equivale a cero eventos.
            'eventCount': len(events) if final else None,
            'bidCreatedEvents': (sum(e.get('eventType') == 'bid-created' for e in events)
                                 if final else None),
            'swapSettledEvents': (sum(e.get('eventType') == 'swap-settled' for e in events)
                                  if final else None),
            'walletAttributed': (wallet or {}).get('attributedObservations'),
            'walletExact': exact if wallet else None,
            'walletAssumed': assumed if wallet else None,
            'walletMeanMs': wallet_stats.get('meanMs'), 'walletP50Ms': wallet_stats.get('p50Ms'),
            'walletP95Ms': wallet_stats.get('p95Ms'),
            'walletTimeoutCount': (wallet or {}).get('timeoutCount'),
            'wallet429Count': (wallet or {}).get('http429Count'),
            'readyForEconomicComparison': 'YES' if comparable else 'NO',
            'qualityFlags': ';'.join(sorted(set(quality))),
            'notes': slot.get('notes', ''),
        }
        results.append({k: _string(row.get(k)) for k in CYCLE_FIELDS})

    by_span = defaultdict(list)
    for row in results:
        by_span[int(row['plannedSpanSeconds'])].append(row)
    spans = []
    for span, group in sorted(by_span.items()):
        eligible = [r for r in group if r['readyForEconomicComparison'] == 'YES']
        def avg(key, *, values=eligible):
            series = [float(v[key]) for v in values if v[key]]
            return f'{mean(series):.6f}' if series else ''
        def med(key):
            series = [float(v[key]) for v in eligible if v[key]]
            return f'{median(series):.6f}' if series else ''
        saving_rates = []
        for r in eligible:
            base, saving = decimal(r['baselineCost']), decimal(r['savingsVsBaseline'])
            if base and base > 0 and saving is not None:
                saving_rates.append(float(saving / base))
        spans.append({
            'spanSeconds': span, 'planned': len(group),
            'captured': sum(bool(r['finalCaptureAt']) for r in group),
            'final': sum(r['metricStatus'] == 'FINAL' for r in group),
            'economicallyComparable': len(eligible),
            'meanCoverage': avg('coverageRate'), 'medianCoverage': med('coverageRate'),
            'meanSavingsRate': f'{mean(saving_rates):.6f}' if saving_rates else '',
            'withWalletLatency': sum(bool(r['walletAttributed'] and int(r['walletAttributed']) > 0) for r in group),
            'meanWalletP95Ms': avg('walletP95Ms', values=group),
        })
    counts = Counter(flag for r in results for flag in r['qualityFlags'].split(';') if flag)
    text = [
        '# E2-067 — Registro de ejecución real de spans (RF06)', '',
        '**Fuente:** capturas de API real y telemetría E2-027; nunca datos simulados.',
        f'**Planificados:** {len(results)} · **Capturas finales:** {sum(bool(r["finalCaptureAt"]) for r in results)}'
        f' · **Comparables económicamente:** {sum(r["readyForEconomicComparison"] == "YES" for r in results)}.',
        '', '## Desempeño observado por span (descriptivo)', '',
        '| Span (s) | Ejecutados/capturados | FINAL | Comparables | Cobertura media | Ahorro relativo medio | Ciclos con latencia |',
        '|---:|---:|---:|---:|---:|---:|---:|',
    ]
    for s in spans:
        text.append(f'| {s["spanSeconds"]} | {s["captured"]}/{s["planned"]} | '
                    f'{s["final"]} | {s["economicallyComparable"]} | '
                    f'{s["meanCoverage"] or "—"} | {s["meanSavingsRate"] or "—"} | '
                    f'{s["withWalletLatency"]} |')
    text += ['', '## Calidad de evidencia y factores externos', '']
    text += ([f'- `{name}`: {value} ciclo(s).' for name, value in sorted(counts.items())]
             if counts else ['- Ninguna alerta de calidad.'])
    text += ['', '- Una captura de `/market/book` describe el feed al momento de la lectura; no es un historial completo.',
             '- La liquidez y el precio efectivo se comparan usando capturas temporales etiquetadas. Las rutas sin tarifa conocida no se inventan.',
             '- La latencia de Wallet se correlaciona por `cycleId` o `idpk` cuando es posible. `ASSUMED_ISOLATED` se identifica separadamente.',
             '- Revisar `environment`, `deploySha`, el régimen externo, los eventos y el estado final del costo antes de comparar ciclos.',
             '- El comportamiento entre ciclos es observacional. Distintos precios, rutas, competencia o latencia pueden explicar diferencias.',
             '- No se afirma que un span sea mejor sin observaciones suficientes y controles de condiciones externas.',
             '', '## Acciones pendientes', '',
             '1. Ejecutar los slots sin `cycleId` y capturar su evidencia real.',
             '2. Repetir capturas de ciclos cuyo costo todavía no esté FINAL.',
             '3. Incorporar latencias Wallet con correlación explícita o atribución manual documentada.',
             '4. Analizar los mismos ciclos con `qa.e2066.analizar` usando métricas E2-065.', '']
    return results, event_rows, spans, '\n'.join(text)


def main(argv=None):
    parser = argparse.ArgumentParser(description='E2-067: consolidar evidencia real por ciclo')
    parser.add_argument('--plan', default='datos_e2066/plan_spans.csv')
    parser.add_argument('--data-dir', default=str(DATA_DIR))
    parser.add_argument('--out-dir', default='datos_e2067/resultados')
    args = parser.parse_args(argv)
    plan = load_plan(args.plan)
    rows, events, spans, report = build_report(plan, Path(args.data_dir))
    out = Path(args.out_dir)
    for filename, content in (
        ('registro_ciclos.csv', csv_string(rows, CYCLE_FIELDS)),
        ('eventos_por_ciclo.csv', csv_string(events, EVENT_FIELDS)),
        ('resumen_por_span.csv', csv_string(spans, SPAN_FIELDS)),
        ('informe_E2_067.md', report),
    ):
        write_atomic(out / filename, content)
    print(f'Reporte real generado en {out}: {len(rows)} slots, '
          f'{sum(r["finalCaptureAt"] != "" for r in rows)} capturas finales.')
    print('Sin capturas reales, el reporte solo muestra pendientes; no se inventan resultados.')


if __name__ == '__main__':
    main()
