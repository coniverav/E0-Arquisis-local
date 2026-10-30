"""Extrae latencia Wallet de logs E2-027 o CSV New Relic exportado.

Atribución de observaciones:
- DIRECT_CYCLE_ID: solo si el registro contiene un cycleId explícito;
- KNOWN_ID_MATCH: solo si idpk coincide con evento persistido del ciclo;
- ASSUMED_ISOLATED: únicamente con flag explícito; NO es correlación exacta.

Los logs E2-027 normalmente NO tienen cycleId; tampoco todas las peticiones
incluyen idpk. Nunca atribuir por horario solamente sin consentimiento.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean

from .comun import DATA_DIR, aware_datetime, read_json, save_json, wallet_path, iso_now, decimal

LOG_FIELDS = re.compile(r'([A-Za-z][A-Za-z0-9]*)=([^\s]+)')
ISO_PREFIX = re.compile(r'\b(\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d))\b')
ALLOWED_ENDPOINTS = frozenset({'balance', 'statement', 'swaps_propose', 'swaps_accept',
                               'swaps_cancel', 'swaps_get', 'other'})


def read_wallet_observations(filepath: Path) -> tuple[list[dict], int]:
    """Devuelve un registro por operación HTTP; no almacena el cuerpo del log."""
    observations = []
    invalid = 0
    if filepath.suffix.lower() == '.csv':
        with filepath.open(encoding='utf-8-sig', newline='') as handle:
            rows = list(csv.DictReader(handle))
    else:
        rows = []
        with filepath.open(encoding='utf-8', errors='replace') as handle:
            for line in handle:
                mark = line.find('wallet_http ')
                if mark < 0:
                    continue
                fields = dict(LOG_FIELDS.findall(line[mark + len('wallet_http '):]))
                timestamp = ISO_PREFIX.search(line[:mark])
                fields['timestamp'] = timestamp.group(1).replace(' ', 'T') if timestamp else ''
                rows.append(fields)
    for row in rows:
        if not isinstance(row, dict):
            invalid += 1
            continue
        val = decimal(row.get('latencyMs'))
        endpoint = row.get('endpoint')
        if val is None or val < 0 or endpoint not in ALLOWED_ENDPOINTS:
            invalid += 1
            continue
        observations.append({
            'latencyMs': float(val), 'endpoint': endpoint,
            'outcome': str(row.get('outcome', 'unknown'))[:64],
            'idpk': str(row.get('idpk', '')).strip(),
            'cycleId': str(row.get('cycleId', '')).strip(),
            'timestamp': str(row.get('timestamp') or row.get('createdAt') or '').strip(),
        })
    return observations, invalid


def percentile(values: list[float], p: float) -> float | None:
    """Percentil lineal de posiciones (n-1), sin interpolación entre ciclos."""
    if not values:
        return None
    if not 0 <= p <= 100:
        raise ValueError('Percentil fuera de rango')
    s = sorted(values)
    index = (len(s) - 1) * p / 100.0
    low = int(index)
    high = min(low + 1, len(s) - 1)
    return round(s[low] + (s[high] - s[low]) * (index - low), 2)


def aggregate_wallet(observations: list[dict], *, evidence: dict,
                     assume_isolated: bool = False) -> dict:
    cid = evidence['cycleId']
    known_ids = {str(e['ref']) for e in evidence.get('events', [])
                 if e.get('ref') and str(e['ref']) != 'None'}
    # Si existen horas conocidas y logs timestamped, filtrar lo que es
    # definitivamente ajeno a la vida de este ciclo, incluso en modo aislado.
    started = aware_datetime(evidence['externalFactors'].get('marketOpensAt'))
    end = aware_datetime(evidence['cycle'].get('validUntil'))
    end = end or aware_datetime(evidence['externalFactors'].get('closesAt'))

    included, discarded = [], Counter()
    for obs in observations:
        observed_time = aware_datetime(obs.get('timestamp'))
        if observed_time and started and observed_time < started:
            discarded['OUTSIDE_CYCLE_WINDOW'] += 1
            continue
        if observed_time and end and observed_time > end:
            discarded['OUTSIDE_CYCLE_WINDOW'] += 1
            continue
        if obs['cycleId']:
            if obs['cycleId'] != cid:
                discarded['OTHER_CYCLE_ID'] += 1
                continue
            method = 'DIRECT_CYCLE_ID'
        elif obs['idpk'] and obs['idpk'] in known_ids:
            method = 'KNOWN_ID_MATCH'
        elif assume_isolated:
            method = 'ASSUMED_ISOLATED'
        else:
            discarded['NOT_ATTRIBUTABLE'] += 1
            continue
        included.append((obs, method))

    by_endpoint = defaultdict(list)
    for item, attribution in included:
        by_endpoint[item['endpoint']].append(item['latencyMs'])
    return {
        'schemaVersion': 1, 'source': 'REAL_WALLET_TELEMETRY_E2027',
        'cycleId': cid, 'capturedAt': iso_now(),
        'attribution': dict(sorted(Counter(m for _, m in included).items())),
        'qualityNote': ('ASSUMED_ISOLATED requires manually filtered, cycle-exclusive logs; '
                        'overlapping market cycles make this unverified.'),
        'totalObservations': len(observations), 'attributedObservations': len(included),
        'excludedCounts': dict(sorted(discarded.items())),
        'timeoutCount': sum(item['outcome'] == 'timeout' for item, _ in included),
        'http429Count': sum(item['outcome'] == 'http_429' for item, _ in included),
        'summary': {
            'meanMs': round(mean([item['latencyMs'] for item, _ in included]), 2)
                      if included else None,
            'p50Ms': percentile([item['latencyMs'] for item, _ in included], 50),
            'p95Ms': percentile([item['latencyMs'] for item, _ in included], 95),
            'maxMs': round(max([item['latencyMs'] for item, _ in included]), 2)
                      if included else None,
        },
        'byEndpoint': {endpoint: {
            'count': len(values), 'p50Ms': percentile(values, 50),
            'p95Ms': percentile(values, 95), 'meanMs': round(mean(values), 2),
        } for endpoint, values in sorted(by_endpoint.items())},
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description='E2-067: latencias wallet desde E2-027 (sin exponer logs)')
    parser.add_argument('--slot', required=True, type=int)
    parser.add_argument('--plan', default='datos_e2066/plan_spans.csv')
    parser.add_argument('--data-dir', default=str(DATA_DIR))
    parser.add_argument('--file', required=True, help='Exportación de logs wallet_http o CSV New Relic')
    parser.add_argument('--assume-isolated', action='store_true',
                        help='Declaro que el archivo contiene SOLO solicitudes del ciclo indicado')
    parser.add_argument('--refresh', action='store_true')
    args = parser.parse_args(argv)
    from qa.e2066.comun import load_plan
    from .comun import evidence_path, row_for_slot
    slot_row = row_for_slot(load_plan(args.plan), args.slot)
    cid = slot_row['cycleId']
    evidence_file = evidence_path(Path(args.data_dir), args.slot, cid, 'final')
    if not evidence_file.exists():
        parser.error('Falta captura final de ciclo; ejecutar qa.e2067.capturar --phase final')
    evidence = read_json(evidence_file)
    if evidence.get('cycleId') != cid:
        parser.error('Evidence incompatible con el plan')
    infile = Path(args.file)
    outfile = wallet_path(Path(args.data_dir), cid)
    if outfile.exists() and not args.refresh:
        parser.error(f'Ya existe {outfile}; usar --refresh para recalcular')
    observations, invalid = read_wallet_observations(infile)
    result = aggregate_wallet(observations, evidence=evidence,
                              assume_isolated=args.assume_isolated)
    result['sourceSha256'] = hashlib.sha256(infile.read_bytes()).hexdigest()
    result['invalidRecords'] = invalid
    save_json(outfile, result, replace=args.refresh)
    print(f'Latencias guardadas: {outfile}. Atribuidas: {result["attributedObservations"]} / {result["totalObservations"]}.')
    if result['attributedObservations'] == 0:
        print('No hay coincidencias verificables. Para logs aislados, usar --assume-isolated solo si corresponde.')
    if result['attribution'].get('ASSUMED_ISOLATED'):
        print('AVISO: atribución MANUAL, no equivalente a correlación por cycleId o idpk.')
    print('No se guardan URLs, headers, idpk de Wallet ni payloads originales.')


if __name__ == '__main__':
    main()
