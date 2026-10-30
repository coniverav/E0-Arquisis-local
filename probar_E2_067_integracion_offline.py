#!/usr/bin/env python3
"""Prueba de integración OFFLINE para E2-067 y su enlace E2-066/E2-065.

Ejecuta los CLIs reales contra un servidor HTTP simulado en 127.0.0.1,
con logs Wallet E2-027 sintéticos. NO usa Docker, Postgres, EC2 ni Wallet real.
NO modifica datos_e2066/ ni datos_e2067/ del repositorio.

Uso: python3 probar_E2_067_integracion_offline.py
     python3 probar_E2_067_integracion_offline.py --output-dir /tmp/e2067-mi-prueba
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from urllib.parse import parse_qs, unquote, urlsplit
from urllib.request import urlopen
from uuid import NAMESPACE_DNS, uuid5


SECRET = 'SECRETO_PRUEBA_QUE_NO_DEBE_APARECER'
MOCK_COMMIT = 'SOLO_MOCK_E2067_NO_PRODUCCION'
MOCK_NOTE = 'SIMULACION OFFLINE - API y Wallet simuladas. PROHIBIDO reportar como resultados reales.'


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def identifier(slot: int, kind: str) -> str:
    return str(uuid5(NAMESPACE_DNS, f'e2067-offline-mock-{slot}-{kind}'))


def read_csv(path: Path) -> list[dict]:
    with path.open(encoding='utf-8-sig', newline='') as stream:
        return list(csv.DictReader(stream))


def json_file(path: Path) -> dict:
    return json.loads(path.read_text(encoding='utf-8'))


class FakeEnergyShark:
    """Conjunto de endpoints de solo lectura; datos y fallas totalmente locales."""

    def __init__(self, plan: list[dict], base: datetime):
        self.base = base
        self.rows = {f'e2067-offline-{int(row["slot"]):03d}': row for row in plan[:5]}
        self.phase = 'early'
        self.fail_once: set[tuple[str, str]] = set()
        self.alter_history_cycle_for: str | None = None
        self.requests: list[tuple[str, str]] = []
        self.server = self._server()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def _server(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urlsplit(self.path)
                path = parsed.path
                q = parse_qs(parsed.query)
                owner.requests.append(('GET', self.path))
                require(not self.headers.get('Authorization'), 'No debe transmitirse un JWT real al mock')
                cycle = q.get('cycleId', [''])[0]
                if path.startswith('/cycles/') and path.endswith('/history'):
                    cycle = unquote(path[len('/cycles/'):-len('/history')].rstrip('/'))
                    if cycle not in owner.rows:
                        return self._send(404, {'detail': 'Cycle not found'})
                    if ('history503', cycle) in owner.fail_once:
                        owner.fail_once.remove(('history503', cycle))
                        return self._send(503, {'detail': 'Falla sintética temporal'})
                    item = owner.history(cycle)
                    if owner.alter_history_cycle_for == cycle:
                        item['cycleId'] = 'CYCLE-ID-INCORRECTO'
                    return self._send(200, item)
                if path == '/market/cycle-metrics':
                    if ('metrics503', cycle) in owner.fail_once:
                        owner.fail_once.remove(('metrics503', cycle))
                        return self._send(503, {'detail': 'Falla temporal sintética'})
                    metric = owner.metric(cycle)
                    return self._send(200, metric) if metric else self._send(404, {'detail': 'No metric'})
                if path == '/market/cycle-metrics/compare':
                    available = [owner.metric(key, force_final=True) for key in owner.rows]
                    available = [item for item in available if item]
                    status = q.get('status', [None])[0]
                    if status:
                        available = [item for item in available if item['status'] == status]
                    limit = int(q.get('limit', ['50'])[0])
                    offset = int(q.get('offset', ['0'])[0])
                    return self._send(200, {'total': len(available), 'offset': offset,
                                            'limit': limit, 'items': available[offset:offset + limit]})
                if path == '/market/book':
                    if cycle not in owner.rows or cycle.endswith('003'):
                        return self._send(404, {'detail': 'No book'})
                    return self._send(200, owner.book(cycle))
                return self._send(404, {'detail': 'Unknown endpoint'})

            def do_POST(self):
                owner.requests.append(('POST', self.path))
                return self._send(405, {'detail': 'Mock READ ONLY'})

            def _send(self, status: int, payload: dict):
                data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except BrokenPipeError:
                    pass

            def log_message(self, *_args):
                return

        return ThreadingHTTPServer(('127.0.0.1', 0), Handler)

    def start(self):
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def history(self, cycle_id: str) -> dict:
        slot = int(self.rows[cycle_id]['slot'])
        b = self.base
        plus = lambda minutes: iso(b + timedelta(minutes=minutes))
        final = self.phase == 'final'
        bids = [
            {'idpk': identifier(slot, 'bid-rejected'), 'direction': 'OUTGOING',
             'status': 'REJECTED' if final else 'PENDING', 'quantity': 20,
             'createdAt': plus(1), 'respondedAt': plus(3) if final else None,
             'rejectionReason': 'QUANTITY_UNAVAILABLE' if final else None,
             'swapToken': SECRET},
        ]
        if final:
            bids.append({'idpk': identifier(slot, 'bid-accepted'), 'direction': 'OUTGOING',
                         'status': 'ACCEPTED', 'quantity': 70, 'createdAt': plus(2),
                         'respondedAt': plus(4), 'swapId': f'mock-swap-{slot}',
                         'swapToken': SECRET})
        return {
            'cycleId': cycle_id,
            'statusIdpk': identifier(slot, 'status'),
            'statusStatement': {'energy': {'consumption': 1000, 'generationCapacity': 900},
                                'market': {'imbalancePrice': 15 + slot}, 'jwt': SECRET},
            'strategyVersion': 100 + slot,
            'schedulerState': 'CLOSED' if final else 'MARKET_OPEN',
            'createdAt': plus(-3), 'marketOpensAt': plus(0),
            'marketLastBidAt': plus(8), 'marketClosesAt': plus(10),
            'validUntil': plus(20), 'closedAt': plus(11) if final else None,
            'bids': bids,
            'swaps': ([{'idpk': identifier(slot, 'swap'), 'swapId': f'mock-swap-{slot}',
                        'role': 'ACCEPTOR', 'status': 'SETTLED',
                        'createdAt': plus(4), 'settledAt': plus(6), 'swapToken': SECRET}]
                      if final else []),
            'trades': ([{'swapId': f'mock-swap-{slot}', 'recordedAt': plus(7),
                         'transportTotal': '30.00', 'apiKey': SECRET}]
                       if final else []),
            'settlements': ([{'settlementId': f'mock-settlement-{slot}',
                              'recordedAt': plus(10.5)}] if final and slot != 3 else []),
            'offerRevisions': [
                {'msgId': identifier(slot, 'offer'), 'type': 'offer',
                 'createdAt': plus(1), 'publishedAt': plus(1.1), 'token': SECRET},
                *([{'msgId': identifier(slot, 'offer-updated'), 'type': 'offer-updated',
                    'createdAt': plus(5), 'publishedAt': plus(5.1)}] if final else []),
            ],
            'ledger': ([
                {'idpk': identifier(slot, 'ledger-swap'), 'operationType': 'SWAP_SETTLED',
                 'appliedAt': plus(6), 'details': {'api_key': SECRET}},
                {'idpk': identifier(slot, 'ledger-transport'), 'operationType': 'SWAP_TRANSPORT',
                 'appliedAt': plus(7)},
                *([{'idpk': identifier(slot, 'ledger-settlement'),
                    'operationType': 'IMBALANCE_SETTLEMENT', 'appliedAt': plus(10.5)}]
                  if slot != 3 else []),
            ] if final else []),
            'negotiationReports': ([{'idpk': identifier(slot, 'report'),
                                    'createdAt': plus(11), 'status': 'PUBLISHED'}]
                                   if final else []),
        }

    def metric(self, cycle_id: str, *, force_final: bool = False) -> dict | None:
        if cycle_id not in self.rows or cycle_id.endswith('005'):
            return None  # Mide el caso METRICS_NOT_READY.
        slot = int(self.rows[cycle_id]['slot'])
        row = self.rows[cycle_id]
        final = force_final or self.phase == 'final'
        complete = final and slot != 3
        real_cost = [None, '900.00', '1150.00', None, '1020.00'][slot]
        baseline = [None, '1500.00', '1800.00', '1500.00', '1600.00'][slot]
        return {
            'cycleId': cycle_id, 'strategyVersion': 100 + slot,
            'spanSeconds': int(row['spanSeconds']) + (30 if slot == 4 else 0),
            'selectionPolicy': row['selectionPolicy'],
            'acceptancePolicy': row['acceptancePolicy'],
            'deficitKwh': 100, 'boughtKwh': 70 if final else 0,
            'coveredKwh': 70 if final else 0,
            'coverageRate': '0.70' if final else '0',
            'rejectedBids': 1 if final else 0, 'rejectedIncomingBids': 0,
            'buyCost': (str(round(float(real_cost) - 130, 2)) if complete
                        else '770.00' if final else '0.00'),
            'transportCost': '30.00' if final else '0.00',
            'settlementCost': '100.00' if complete else None,
            'realCost': real_cost if complete else None,
            'baselineCost': baseline,
            'baselineSource': 'SETTLEMENT' if complete else 'STATUS_ESTIMATE',
            'savingsVsBaseline': (str(round(float(baseline) - float(real_cost), 2))
                                  if complete else None),
            'missingTransportTrades': 0, 'status': 'FINAL' if complete else (
                'AWAITING_SETTLEMENT' if final else 'IN_PROGRESS'),
            'createdAt': iso(self.base), 'updatedAt': iso(self.base + timedelta(minutes=11)),
            'debugSecret': SECRET,
        }

    def book(self, cycle_id: str) -> dict:
        slot = int(self.rows[cycle_id]['slot'])
        early = self.phase == 'early'
        at = iso(self.base + timedelta(minutes=2 if early else 12))
        disabled = slot == 4
        offers = [
            {'cityId': 'MET', 'offerId': f'mock-offer-{slot}-met',
             'remaining': 110 if early else 20, 'state': 'ACTIVE',
             'pricePerEnergy': '7.00' if early else '9.00',
             'effectivePricePerEnergy': '9.00' if early else '11.00',
             'routeEnabled': True, 'distanceMeters': '1000',
             'lastEventAt': at, 'swapToken': SECRET},
            {'cityId': 'COR', 'offerId': f'mock-offer-{slot}-cor',
             'remaining': 50 if early else 0, 'state': 'ACTIVE' if early else 'WITHDRAWN',
             'pricePerEnergy': '8.00', 'effectivePricePerEnergy': '15.00',
             'routeEnabled': False if disabled else True, 'distanceMeters': '2000',
             'lastEventAt': at, 'X-Api-Key': SECRET},
        ]
        return {'cycleId': cycle_id, 'asOf': at, 'marketClosed': not early,
                'offers': offers, 'debugSecret': SECRET}


def run_command(repo: Path, workspace: Path, module: str, args: list[str],
                *, expect_success: bool = True, fake_clock: str | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.pop('RF06_JWT', None)
    env.pop('E2067_OFFLINE_FAKE_NO_TOKEN', None)
    env['PYTHONPATH'] = str(repo) + os.pathsep + env.get('PYTHONPATH', '')
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    command = [sys.executable]
    if fake_clock is not None:
        code = ('import sys\n'
                'from unittest.mock import patch\n'
                f'from {module.rsplit(".", 1)[0]} import {module.rsplit(".", 1)[1]} as target\n'
                'with patch.object(target, "iso_now", return_value=sys.argv[1]):\n'
                '    target.main(sys.argv[2:])\n')
        command += ['-c', code, fake_clock, *args]
    else:
        command += ['-m', module, *args]
    result = subprocess.run(command, cwd=workspace, env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=25, check=False)
    if expect_success and result.returncode != 0:
        raise AssertionError(f'Fallo inesperado de {module} ({result.returncode}):\n'
                             f'{result.stdout}\n{result.stderr[-3500:]}')
    if not expect_success and result.returncode == 0:
        raise AssertionError(f'{module} debió rechazar la operación, pero terminó correctamente')
    return result


def wallet_logs(base: datetime, slot: int, *, isolated: bool = False) -> str:
    at = lambda minutes: iso(base + timedelta(minutes=minutes))
    key = identifier(slot, 'bid-rejected')
    if isolated:
        return (f'{at(4)} scheduler | INFO wallet_http method=GET endpoint=balance '
                f'outcome=http_200 latencyMs=25.0 header={SECRET}\n')
    return ''.join([
        f'{at(4)} scheduler | INFO wallet_http method=POST endpoint=swaps_accept '
        f'outcome=http_202 latencyMs=41.50 idpk={key} key={SECRET}\n',
        f'{at(5)} scheduler | INFO wallet_http method=GET endpoint=swaps_get '
        f'outcome=timeout latencyMs=350.00 cycleId=e2067-offline-{slot:03d}\n',
        f'{at(6)} scheduler | INFO wallet_http method=POST endpoint=swaps_accept '
        f'outcome=http_429 latencyMs=80.00 idpk={key}\n',
        f'{at(7)} scheduler | INFO wallet_http method=GET endpoint=balance '
        f'outcome=http_200 latencyMs=10.00 idpk=unknown-other-cycle\n',
        f'{at(-3)} scheduler | INFO wallet_http method=GET endpoint=balance '
        f'outcome=http_200 latencyMs=10.00 idpk={key}\n',
        '2026-10-30T00:00:00Z scheduler | INFO wallet_http method=GET '
        'endpoint=balance outcome=http_200 latencyMs=NOT_NUMERIC\n',
    ])


def check_output(workspace: Path, plan: list[dict], base: datetime, mock: FakeEnergyShark) -> list[str]:
    data = workspace / 'datos_e2067_mock'
    results = data / 'resultados'
    rows = read_csv(results / 'registro_ciclos.csv')
    events = read_csv(results / 'eventos_por_ciclo.csv')
    spans = read_csv(results / 'resumen_por_span.csv')
    require(len(rows) == 5, 'Se esperaban cinco slots planificados')
    indexed = {int(row['slot']): row for row in rows}
    require(len(indexed) == 5, 'No puede haber slots duplicados')
    for n in (1, 2):
        require(indexed[n]['readyForEconomicComparison'] == 'YES',
                f'Slot {n} debería ser comparable con costo confirmado: {indexed[n]["qualityFlags"]}')
        require(indexed[n]['walletExact'] == '3', 'Deben existir tres latencias bien correlacionadas')
        require(indexed[n]['walletTimeoutCount'] == '1', 'Debe contabilizar el timeout de Wallet')
        require(indexed[n]['wallet429Count'] == '1', 'Debe contabilizar HTTP 429')
        require(indexed[n]['earlyMedianEffectivePrice'] != indexed[n]['finalMedianEffectivePrice'],
                'Cambio de precio entre snapshots no registrado')
        require(indexed[n]['eventCount'] != '', 'Sin eventos finales guardados')
    require(indexed[3]['readyForEconomicComparison'] == 'NO', 'Pendiente no debe ser comparable')
    require('METRICS_NOT_FINAL' in indexed[3]['qualityFlags'], 'Faltó marcar liquidación pendiente')
    require('BOOK_UNAVAILABLE' in indexed[3]['qualityFlags'], 'Faltó marcar libro no disponible')
    require('WALLET_LATENCY_ASSUMED_ISOLATED' in indexed[3]['qualityFlags'],
            'Atribución manual debe etiquetarse explícitamente')
    require(indexed[4]['readyForEconomicComparison'] == 'NO', 'Span incorrecto no comparable')
    require('SPAN_MISMATCH_AD6' in indexed[4]['qualityFlags'], 'Faltó detectar span incorrecto')
    require(indexed[4]['earlyDisabledRoutesShare'] != '0', 'Ruta deshabilitada no registrada')
    require(indexed[5]['readyForEconomicComparison'] == 'NO', 'Sin captura final no comparable')
    require('FINAL_SNAPSHOT_MISSING' in indexed[5]['qualityFlags'], 'No se marcó captura final ausente')
    require('METRICS_NOT_READY' in indexed[5]['qualityFlags'], 'No se marcó falta de métricas')
    require(sum(row['readyForEconomicComparison'] == 'YES' for row in rows) == 2,
            'Solo dos slots deben ser comparables')
    require(len(events) > 14, f'Deben existir suficientes eventos y se encontraron {len(events)}')
    require(set(int(row['spanSeconds']) for row in spans) == {int(row['spanSeconds']) for row in plan[:5]},
            'Resumen debe contener todos los spans')
    require(all(int(row['economicallyComparable']) in (0, 1) for row in spans),
            'Agregados de spans inconsistentes')
    evidence = json_file(data / 'evidencias' / '001_e2067-offline-001' / 'final.json')
    require(evidence['externalFactors']['bookSnapshotIsHistoricalTimeline'] is False,
            'El snapshot del feed NO debe declararse historial completo')
    require(evidence['source'] == 'REAL_API_READONLY', 'Se debe ejercer la misma ruta de captura real')
    require(MOCK_COMMIT == evidence['deployment']['commit'], 'La prueba debe quedar rotulada como mock')
    wallet = json_file(data / 'wallet' / 'e2067-offline-001.json')
    require(wallet['attribution'].get('KNOWN_ID_MATCH') == 2, 'Correlación idpk no registrada')
    require(wallet['attribution'].get('DIRECT_CYCLE_ID') == 1, 'Correlación cycleId no registrada')
    require(wallet['excludedCounts'].get('NOT_ATTRIBUTABLE') == 1, 'Observación no atribuible incluida')
    require(wallet['excludedCounts'].get('OUTSIDE_CYCLE_WINDOW') == 1, 'Observación fuera de ventana incluida')
    require(wallet['invalidRecords'] == 1, 'Registro inválido de latencia no contado')
    # Privacidad: los informes y las evidencias procesadas nunca deben copiar secretos de los mocks.
    for directory in (data / 'evidencias', data / 'wallet', results):
        for path in directory.rglob('*'):
            if path.is_file():
                require(SECRET not in path.read_text(encoding='utf-8'),
                        f'Fuga de secreto sintético en {path.relative_to(workspace)}')
    require(all(method == 'GET' for method, _ in mock.requests),
            'Se observó una operación de escritura contra el backend simulado')
    return [f'5 slots de prueba, 4 cierres capturados, 2 económicamente comparables',
            f'{len(events)} eventos cronológicos; sin duplicación',
            'Latencias: correlación exacta, timeout, 429, excluidas y atribución manual diferenciadas',
            'Errores controlados: 503, 404, identidad incorrecta y falta de cierre',
            'Privacidad: sin copiar tokens/secreto; ninguna operación POST']


def exercise(repo: Path, workspace: Path) -> list[str]:
    data = workspace / 'datos_e2067_mock'
    plan_path = workspace / 'plan_mock_e2066.csv'
    base = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=1)
    run_command(repo, workspace, 'qa.e2066.planificar',
                ['--blocks', '1', '--output', str(plan_path)])
    plan = read_csv(plan_path)
    require(len(plan) == 5, 'Plan E2-066 debe contener cinco spans')

    mock = FakeEnergyShark(plan, base)
    mock.start()
    try:
        for n, regime in enumerate(('NORMAL', 'HIGH_PRICE', 'LOW_LIQUIDITY',
                                    'ROUTE_DISABLED', 'LATE_OFFERS'), start=1):
            run_command(repo, workspace, 'qa.e2066.registrar_ciclo',
                        ['--plan', str(plan_path), '--slot', str(n),
                         '--cycle-id', f'e2067-offline-{n:03d}', '--regime', regime,
                         '--notes', MOCK_NOTE])
        plan = read_csv(plan_path)
        mock.phase = 'early'
        early_time = iso(base + timedelta(minutes=2))
        final_time = iso(base + timedelta(minutes=12))
        args = lambda n, phase: [
            '--slot', str(n), '--phase', phase, '--plan', str(plan_path),
            '--data-dir', str(data), '--base-url', mock.url,
            '--environment', 'local', '--deploy-sha', MOCK_COMMIT,
            '--notes', MOCK_NOTE, '--token-env', 'E2067_OFFLINE_FAKE_NO_TOKEN',
        ]
        # Simular backend 503, confirmar fallo limpio, y reintentar sin cambiar información.
        mock.fail_once.add(('history503', 'e2067-offline-005'))
        failed = run_command(repo, workspace, 'qa.e2067.capturar', args(5, 'early'),
                             expect_success=False, fake_clock=early_time)
        require('503' in (failed.stdout + failed.stderr), 'HTTP 503 debería propagarse')
        require(not (data / 'evidencias' / '005_e2067-offline-005' / 'early.json').exists(),
                'Error HTTP no debe escribir evidencia parcial')
        for n in range(1, 6):
            run_command(repo, workspace, 'qa.e2067.capturar', args(n, 'early'),
                        fake_clock=early_time)
        # El CLI evita sobrescrituras accidentales de evidencia existente.
        duplicate = run_command(repo, workspace, 'qa.e2067.capturar', args(1, 'early'),
                                fake_clock=early_time, expect_success=False)
        require('refresh' in (duplicate.stdout + duplicate.stderr),
                'Captura repetida debería exigir --refresh')
        # Sin captura final, no se deben importar latencias de Wallet.
        before_final = run_command(repo, workspace, 'qa.e2067.latencias',
                                   ['--slot', '1', '--plan', str(plan_path),
                                    '--data-dir', str(data), '--file', str(plan_path)],
                                   expect_success=False)
        require('Falta captura final' in (before_final.stdout + before_final.stderr),
                'Latencias antes del cierre deben rechazarse')
        mock.phase = 'final'
        mock.alter_history_cycle_for = 'e2067-offline-004'
        mismatch = run_command(repo, workspace, 'qa.e2067.capturar', args(4, 'final'),
                               fake_clock=final_time, expect_success=False)
        require('otro cycleId' in (mismatch.stdout + mismatch.stderr),
                'Captura de identidad ajena debería fallar: ' + (mismatch.stdout + mismatch.stderr)[-1200:])
        mock.alter_history_cycle_for = None
        for n in range(1, 5):
            run_command(repo, workspace, 'qa.e2067.capturar', args(n, 'final'),
                        fake_clock=final_time)
        # --refresh es explícito y debe mantener un único archivo por slot/fase.
        run_command(repo, workspace, 'qa.e2067.capturar', args(1, 'final') + ['--refresh'],
                    fake_clock=final_time)

        logs_dir = workspace / 'logs_sinteticos'
        logs_dir.mkdir()
        for n in (1, 2, 4):
            filepath = logs_dir / f'wallet_{n}.log'
            filepath.write_text(wallet_logs(base, n), encoding='utf-8')
            run_command(repo, workspace, 'qa.e2067.latencias',
                        ['--slot', str(n), '--plan', str(plan_path),
                         '--data-dir', str(data), '--file', str(filepath)])
        manual_file = logs_dir / 'wallet_3_solo_ciclo.log'
        manual_file.write_text(wallet_logs(base, 3, isolated=True), encoding='utf-8')
        run_command(repo, workspace, 'qa.e2067.latencias',
                    ['--slot', '3', '--plan', str(plan_path), '--data-dir', str(data),
                     '--file', str(manual_file), '--assume-isolated'])
        # Importar dos veces requiere consentimiento expreso para reemplazar.
        dup_wallet = run_command(repo, workspace, 'qa.e2067.latencias',
                                 ['--slot', '1', '--plan', str(plan_path), '--data-dir', str(data),
                                  '--file', str(logs_dir / 'wallet_1.log')], expect_success=False)
        require('refresh' in (dup_wallet.stdout + dup_wallet.stderr),
                'Telemetría previa debería protegerse contra overwrite')
        run_command(repo, workspace, 'qa.e2067.consolidar',
                    ['--plan', str(plan_path), '--data-dir', str(data),
                     '--out-dir', str(data / 'resultados')])

        # Integración con recolección E2-066 y vista E2-065, incluyendo paginación.
        metrics_path = workspace / 'metricas_simuladas_e2065.json'
        run_command(repo, workspace, 'qa.e2066.recolectar',
                    ['--base-url', mock.url, '--output', str(metrics_path),
                     '--token-env', 'E2067_OFFLINE_FAKE_NO_TOKEN'])
        require(json_file(metrics_path)['total'] == 4,
                'Recolección mock debe recoger los cuatro ciclos con métricas')
        # 2 páginas en el endpoint de comparación, verificadas directamente.
        from urllib.parse import urlencode
        for offset, expected in ((0, 2), (2, 2)):
            with urlopen(f'{mock.url}/market/cycle-metrics/compare?'
                         + urlencode({'limit': 2, 'offset': offset}), timeout=3) as response:
                page = json.load(response)
            require(len(page['items']) == expected and page['total'] == 4,
                    'Paginación E2-065 inconsistente')
        run_command(repo, workspace, 'qa.e2066.analizar',
                    ['--plan', str(plan_path), '--metrics', str(metrics_path),
                     '--out-dir', str(workspace / 'analisis_e2066_mock')])
        compare_rows = read_csv(workspace / 'analisis_e2066_mock' / 'comparacion_por_ciclo.csv')
        require(sum(r['eligible'] == 'YES' for r in compare_rows) == 2,
                'Comparación E2-066 mock debe excluir pendientes/mismatches/sin métricas')

        points = check_output(workspace, plan, base, mock)
        # Los reportes originales hablan de 'datos reales' porque son los mismos
        # CLIs productivos: anteponer un encabezado inequívoco SOLO al mock.
        for report_path in (
            data / 'resultados' / 'informe_E2_067.md',
            workspace / 'analisis_e2066_mock' / 'informe_RF06.md',
        ):
            original = report_path.read_text(encoding='utf-8')
            report_path.write_text(
                '# PRUEBA SIMULADA — NO SON RESULTADOS REALES DE RF06\n\n'
                '**Fuente de esta ejecución:** servidor HTTP y logs Wallet simulados '
                'en 127.0.0.1; no se conectó a EnergyShark central ni a Wallet.\n\n'
                + original, encoding='utf-8')
        (workspace / 'AVISO_MOCK_NO_RESULTADOS_REALES.txt').write_text(
            'PRUEBA DE INTEGRACION OFFLINE. Todos los cicloIds, swaps, eventos, balances '
            'y latencias son SIMULADOS. No incluir resultados de este directorio en '
            'informes experimentales RF06 ni como evidencia de funcionamiento real de Wallet/EC2.\n',
            encoding='utf-8')
        return points
    finally:
        mock.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description='E2-067: integración local offline con API Wallet simuladas')
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parent,
                        help='Raíz del repositorio con qa/e2066 y qa/e2067 instalados')
    parser.add_argument('--output-dir', type=Path, help='Carpeta NUEVA para conservar los resultados MOCK')
    args = parser.parse_args()
    repo = args.repo.expanduser().resolve()
    for name in ('qa/e2066/planificar.py', 'qa/e2066/registrar_ciclo.py',
                 'qa/e2066/recolectar.py', 'qa/e2067/capturar.py',
                 'qa/e2067/latencias.py', 'qa/e2067/consolidar.py'):
        if not (repo / name).is_file():
            parser.error(f'No encuentro {name}; instala E2-066 y E2-067 y usa --repo RUTA')
    if args.output_dir:
        root = args.output_dir.expanduser().resolve()
        if root.exists():
            parser.error(f'La carpeta {root} ya existe; por seguridad no se sobrescribe')
        root.mkdir(parents=True)
        workspace = root
        cleanup = None
    else:
        cleanup = tempfile.TemporaryDirectory(prefix='e2067_integracion_mock_')
        workspace = Path(cleanup.name)
    try:
        print('E2-067 · Prueba de integración OFFLINE en 127.0.0.1')
        print('Datos de prueba: 100 % SINTÉTICOS. No se toca Wallet, RabbitMQ ni la base real.')
        checks = exercise(repo, workspace)
        print('\n' + '=' * 65)
        print('OK — integración local E2-066 / E2-065(mock) / E2-067')
        for item in checks:
            print('  ✓ ' + item)
        if cleanup:
            print('Datos mock temporales eliminados al terminar.')
        else:
            print('Evidencia MOCK conservada en:', workspace)
            print('  ', workspace / 'datos_e2067_mock' / 'resultados' / 'informe_E2_067.md')
        print('PENDIENTE: solo un mercado real valida conectividad central, Wallet y resultados reales RF06.')
        return 0
    except (AssertionError, subprocess.TimeoutExpired, OSError, ValueError) as exc:
        print('\nERROR en prueba offline:', exc, file=sys.stderr)
        if not cleanup:
            print('Artefactos parciales (MOCK):', workspace, file=sys.stderr)
        return 1
    finally:
        if cleanup:
            cleanup.cleanup()


if __name__ == '__main__':
    sys.exit(main())
