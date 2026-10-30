"""Captura evidence REAL y saneada desde API EnergyShark (solo GET).

NO se almacenan payloads completos, tokens, JWT ni llaves de Wallet.
Los snapshots de libro son estados vistos EN EL INSTANTE de lectura, no
historia completa de ofertas; timestamps de última actualización no prueban
cuándo apareció una oferta por primera vez.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
from decimal import Decimal
from statistics import median
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
import json
import os
from pathlib import Path

from .comun import (
    DATA_DIR, REAL_METRIC_FIELDS, PHASES, aware_datetime, decimal,
    evidence_path, iso_now, keep, row_for_slot, read_json, save_json,
)
from qa.e2066.comun import load_plan


EVENT_SOURCES = (
    ('bids', 'bid-created', 'createdAt', 'idpk'),
    ('bids', 'bid-responded', 'respondedAt', 'idpk'),
    ('swaps', 'swap-created', 'createdAt', 'idpk'),
    ('swaps', 'swap-settled', 'settledAt', 'swapId'),
    ('trades', 'trade-recorded', 'recordedAt', 'swapId'),
    ('settlements', 'imbalance-settlement', 'recordedAt', 'settlementId'),
    ('offerRevisions', 'offer-outbox-created', 'createdAt', 'msgId'),
    ('offerRevisions', 'offer-outbox-published', 'publishedAt', 'msgId'),
    ('ledger', 'ledger-effect', 'appliedAt', 'idpk'),
    ('negotiationReports', 'negotiation-report-created', 'createdAt', 'idpk'),
)


def safe_events(history: dict) -> list[dict]:
    """Cronología por hechos persistidos; sin contenido de wallets ni cuerpos AMQP."""
    events = []
    for source, kind, time_field, ref_field in EVENT_SOURCES:
        records = history.get(source) or []
        if not isinstance(records, list):
            raise ValueError(f'Historial inválido: {source} debe ser lista')
        for record in records:
            if not isinstance(record, dict):
                continue
            timestamp = record.get(time_field)
            if not aware_datetime(timestamp):
                continue
            if kind == 'bid-responded' and not record.get('respondedAt'):
                continue
            if kind == 'swap-settled' and record.get('status') != 'SETTLED':
                continue
            event = {'at': timestamp, 'eventType': kind,
                     'ref': str(record.get(ref_field) or ''), 'source': source}
            if source == 'bids':
                event['direction'] = record.get('direction')
                event['status'] = record.get('status')
                event['quantity'] = record.get('quantity')
                if kind == 'bid-responded':
                    event['reason'] = record.get('rejectionReason')
            elif source == 'swaps':
                event['status'] = record.get('status')
                event['role'] = record.get('role')
            elif source == 'ledger':
                event['operationType'] = record.get('operationType')
            elif source == 'offerRevisions':
                event['messageType'] = record.get('type')
            events.append(event)
    return sorted(events, key=lambda e: (e['at'], e['eventType'], e['ref']))


def external_factors(book: dict | None, history: dict, metrics: dict | None) -> dict:
    """Fotografía observable; NO extrapolar la liquidez de apertura al cierre."""
    start = aware_datetime(history.get('marketOpensAt'))
    lastbid = aware_datetime(history.get('marketLastBidAt'))
    close = aware_datetime(history.get('marketClosesAt'))
    items = (book or {}).get('offers', [])
    has_book = book is not None
    if not isinstance(items, list):
        raise ValueError('Libro inválido: offers debe ser lista')
    real_offers = [x for x in items if isinstance(x, dict) and x.get('state') == 'ACTIVE'
                   and x.get('remaining') is not None and decimal(x.get('remaining')) is not None
                   and decimal(x['remaining']) >= 0]
    quantities = [decimal(x['remaining']) for x in real_offers]
    priced = [decimal(x.get('effectivePricePerEnergy')) for x in real_offers]
    priced = [x for x in priced if x is not None and x >= 0]
    disabled = [x for x in real_offers if x.get('routeEnabled') is False]
    unknown_route = sum(x.get('routeEnabled') is None for x in real_offers)
    deficit = decimal((metrics or {}).get('deficitKwh'))
    amount = sum(quantities, Decimal(0))
    # Esto mide liquidez aparente del feed, no garantiza adjudicación.
    liquidity = (amount / deficit) if deficit and deficit > 0 else None
    return {
        'bookCapturedAt': (book or {}).get('asOf'),
        'bookMarketClosed': (book or {}).get('marketClosed'),
        'bookSnapshotIsHistoricalTimeline': False,
        'offersInSnapshot': len(items) if has_book else None,
        'activeOffersInSnapshot': len(real_offers) if has_book else None,
        'activeRemainingKwhInSnapshot': str(amount) if has_book else None,
        'liquidityRatioAtCapture': str(liquidity) if liquidity is not None else None,
        'medianEffectivePriceAtCapture': str(median(priced)) if priced else None,
        'disabledRoutesShareAtCapture': (str(Decimal(len(disabled)) / len(real_offers))
                                         if real_offers else None),
        'routesWithUnknownPrice': unknown_route,
        'imbalancePrice': ((history.get('statusStatement') or {}).get('market') or {}).get('imbalancePrice')
                          if isinstance((history.get('statusStatement') or {}).get('market'), dict) else None,
        'marketOpensAt': history.get('marketOpensAt'),
        'lastBidAt': history.get('marketLastBidAt'),
        'closesAt': history.get('marketClosesAt'),
        'marketWindowSeconds': int((lastbid - start).total_seconds()) if start and lastbid else None,
        'closeWindowSeconds': int((close - start).total_seconds()) if start and close else None,
    }


def make_evidence(*, slot_row: dict, phase: str, history: dict,
                  metrics: dict | None, book: dict | None, collected_at: str,
                  environment: str, commit: str, notes: str = '') -> dict:
    cycle_id = slot_row['cycleId']
    if history.get('cycleId') != cycle_id:
        raise ValueError('El historial corresponde a otro cycleId')
    if metrics is not None and metrics.get('cycleId') != cycle_id:
        raise ValueError('Las métricas corresponden a otro cycleId')
    if book is not None and book.get('cycleId') != cycle_id:
        raise ValueError('El libro corresponde a otro cycleId')
    if phase not in PHASES:
        raise ValueError('phase inválida')
    events = safe_events(history)
    planned = int(slot_row['spanSeconds'])
    actual = metrics.get('spanSeconds') if metrics else None
    flags = []
    sampled = aware_datetime(collected_at)
    opened = aware_datetime(history.get('marketOpensAt'))
    closed = aware_datetime(history.get('marketClosesAt'))
    if phase == 'early' and sampled and opened and sampled < opened:
        flags.append('EARLY_CAPTURE_BEFORE_OPEN')
    if phase == 'early' and sampled and closed and sampled >= closed:
        flags.append('EARLY_CAPTURE_AFTER_CLOSE')
    if phase == 'final' and sampled and closed and sampled < closed:
        flags.append('FINAL_CAPTURE_BEFORE_CLOSE')
    if not history.get('statusIdpk'):
        flags.append('UNCONFIRMED_CYCLE_STATUS')
    if metrics is None:
        flags.append('METRICS_NOT_READY')
    else:
        if actual != planned:
            flags.append('SPAN_MISMATCH_AD6')
        if metrics.get('strategyVersion') != history.get('strategyVersion'):
            flags.append('STRATEGY_VERSION_MISMATCH')
        if metrics.get('selectionPolicy') != slot_row['selectionPolicy'] or metrics.get('acceptancePolicy') != slot_row['acceptancePolicy']:
            flags.append('POLICY_MISMATCH')
        if phase == 'final' and metrics.get('status') != 'FINAL':
            flags.append('COST_NOT_FINAL')
    if book is None:
        flags.append('BOOK_UNAVAILABLE')
    if not commit or commit.lower() == 'unknown':
        flags.append('DEPLOYMENT_COMMIT_NOT_RECORDED')
    market = external_factors(book, history, metrics)
    counts = dict(sorted(Counter(e['eventType'] for e in events).items()))
    return {
        'schemaVersion': 1, 'source': 'REAL_API_READONLY', 'capturedAt': collected_at,
        'slot': int(slot_row['slot']), 'block': int(slot_row['block']),
        'phase': phase, 'cycleId': cycle_id,
        'deployment': {'environment': environment, 'commit': commit},
        'planned': {'spanSeconds': planned, 'selectionPolicy': slot_row['selectionPolicy'],
                    'acceptancePolicy': slot_row['acceptancePolicy']},
        'metrics': keep(metrics, REAL_METRIC_FIELDS) if metrics else None,
        'cycle': {
            'statusConfirmed': bool(history.get('statusIdpk')),
            'strategyVersion': history.get('strategyVersion'),
            'schedulerState': history.get('schedulerState'),
            'createdAt': history.get('createdAt'),
            'validUntil': history.get('validUntil'),
            'closedAt': history.get('closedAt'),
        },
        'externalFactors': market,
        'marketSnapshot': {
            'captureType': 'CURRENT_LAST_KNOWN_FEED_STATE',
            'offers': [keep(x, ('cityId', 'offerId', 'remaining', 'state',
                                'pricePerEnergy', 'effectivePricePerEnergy',
                                'distanceMeters', 'routeEnabled', 'uncertain',
                                'stale', 'lastEventAt')) for x in (book or {}).get('offers', [])],
        },
        'events': events, 'eventCounts': counts,
        'observedRegimeFromPlan': slot_row.get('observedRegime', ''),
        'notes': notes or slot_row.get('notes', ''),
        'qualityFlags': flags,
    }


def capture(fetch, *, slot_row: dict, phase: str, environment: str,
            commit: str, notes: str = '', now: str | None = None) -> dict:
    """fetch(endpoint) -> dict|None. 404 en métricas/libro no invalida ciclo."""
    cycle_id = slot_row['cycleId']
    history = fetch(f'/cycles/{quote(cycle_id, safe="")}/history')
    if not isinstance(history, dict):
        raise ValueError('Historial de ciclo no disponible')
    metrics = fetch('/market/cycle-metrics?' + urlencode({'cycleId': cycle_id}))
    book = fetch('/market/book?' + urlencode({'cycleId': cycle_id, 'includeInactive': 'true'}))
    return make_evidence(slot_row=slot_row, phase=phase, history=history, metrics=metrics,
                         book=book, collected_at=now or iso_now(), environment=environment,
                         commit=commit, notes=notes)


def main(argv=None):
    parser = argparse.ArgumentParser(description='E2-067: captura de ciclo real, solo lectura')
    parser.add_argument('--plan', default='datos_e2066/plan_spans.csv')
    parser.add_argument('--data-dir', default=str(DATA_DIR))
    parser.add_argument('--slot', type=int, required=True)
    parser.add_argument('--phase', choices=PHASES, required=True)
    parser.add_argument('--base-url', default='http://localhost:8001')
    parser.add_argument('--environment', default='local', choices=('local', 'staging', 'production'))
    parser.add_argument('--deploy-sha', default='unknown')
    parser.add_argument('--notes', default='')
    parser.add_argument('--token-env', default='RF06_JWT')
    parser.add_argument('--timeout', type=float, default=15.0)
    parser.add_argument('--refresh', action='store_true', help='Reemplazar captura previa de esa fase')
    args = parser.parse_args(argv)
    if not args.base_url.startswith(('http://', 'https://')) or args.timeout <= 0:
        parser.error('Base URL o timeout inválidos')
    plan = load_plan(args.plan)
    slot_row = row_for_slot(plan, args.slot)
    out = evidence_path(Path(args.data_dir), args.slot, slot_row['cycleId'], args.phase)
    if out.exists() and not args.refresh:
        parser.error(f'Ya existe {out}; usa --refresh solo si deseas actualizar la evidencia')
    token = os.environ.get(args.token_env)

    def fetch(endpoint):
        url = args.base_url.rstrip('/') + endpoint
        headers = {'Accept': 'application/json'}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        try:
            with urlopen(Request(url, headers=headers), timeout=args.timeout) as response:
                data = json.load(response)
                if not isinstance(data, dict):
                    raise RuntimeError(f'JSON no objeto en {endpoint}')
                return data
        except HTTPError as exc:
            if exc.code == 404 and endpoint.startswith('/market/'):
                return None
            raise RuntimeError(f'HTTP {exc.code} consultando endpoint de ciclo') from exc
        except URLError as exc:
            raise RuntimeError(f'No se pudo consultar backend: {exc.reason}') from exc

    evidence = capture(fetch, slot_row=slot_row, phase=args.phase,
                       environment=args.environment, commit=args.deploy_sha,
                       notes=args.notes)
    save_json(out, evidence, replace=args.refresh)
    print(f'Evidencia REAL ({args.phase}) guardada en {out}.')
    print('Eventos:', len(evidence['events']), '| métricas:',
          (evidence['metrics'] or {}).get('status', 'NO DISPONIBLES'))
    if evidence['qualityFlags']:
        print('Advertencias:', ', '.join(evidence['qualityFlags']))
    print('No se enviaron bids ni se modificó la estrategia; captura HTTP de solo lectura.')


if __name__ == '__main__':
    main()
