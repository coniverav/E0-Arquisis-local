"""E2-067: datos reales etiquetados, fuente segura, correlación y elegibilidad."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import csv
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from qa.e2066.planificar import build_plan
from qa.e2067.capturar import make_evidence, capture, safe_events, external_factors
from qa.e2067.comun import evidence_path, wallet_path, save_json, read_json
from qa.e2067.consolidar import build_report
from qa.e2067.latencias import aggregate_wallet, read_wallet_observations, percentile

NOW = datetime(2026, 10, 29, 12, 15, tzinfo=timezone.utc)


def iso(delta=0):
    return (NOW + timedelta(seconds=delta)).isoformat()


def slot():
    result = build_plan(blocks=1)
    result[0]['cycleId'] = 'cycle-real-001'
    return result[0]


def history():
    return {
        'cycleId': 'cycle-real-001', 'statusIdpk': 'valid-status-idpk',
        'statusStatement': {'market': {'imbalancePrice': 11}, 'password': 'MUST_NOT_LEAK'},
        'strategyVersion': 7, 'schedulerState': 'CLOSED',
        'marketOpensAt': iso(-120), 'marketLastBidAt': iso(240),
        'marketClosesAt': iso(300), 'validUntil': iso(600),
        'createdAt': iso(-240), 'closedAt': iso(300),
        'bids': [{'idpk': 'known-uuid-key', 'direction': 'OUTGOING', 'status': 'REJECTED',
                  'quantity': 10, 'createdAt': iso(-30), 'respondedAt': iso(30),
                  'rejectionReason': 'QUANTITY_UNAVAILABLE', 'secret': 'MUST_NOT_LEAK'}],
        'swaps': [{'idpk': 'swap-local-key', 'swapId': 'swap-settled-id',
                   'role': 'ACCEPTOR', 'status': 'SETTLED', 'createdAt': iso(20),
                   'settledAt': iso(40), 'swapToken': 'MUST_NOT_LEAK'}],
        'trades': [{'swapId': 'swap-settled-id', 'recordedAt': iso(50)}],
        'settlements': [{'settlementId': 'settlement-1', 'recordedAt': iso(310)}],
        'offerRevisions': [{'msgId': 'msg-offer-1', 'type': 'offer',
                            'createdAt': iso(0), 'publishedAt': iso(2)}],
        'ledger': [{'idpk': 'ledger-1', 'operationType': 'SWAP_SETTLED',
                    'appliedAt': iso(40), 'details': {'apiKey': 'MUST_NOT_LEAK'}}],
        'negotiationReports': [],
    }


def metrics():
    row = slot()
    return {
        'cycleId': row['cycleId'], 'strategyVersion': 7,
        'spanSeconds': int(row['spanSeconds']),
        'selectionPolicy': row['selectionPolicy'],
        'acceptancePolicy': row['acceptancePolicy'],
        'deficitKwh': 100, 'boughtKwh': 80, 'coveredKwh': 80,
        'coverageRate': '0.8', 'rejectedBids': 1, 'status': 'FINAL',
        'baselineSource': 'SETTLEMENT', 'realCost': '840.00',
        'baselineCost': '1100.00', 'savingsVsBaseline': '260.00',
        'password': 'MUST_NOT_LEAK',
    }


def book():
    return {
        'cycleId': 'cycle-real-001', 'marketClosed': False,
        'asOf': iso(), 'offers': [
            {'cityId': 'COR', 'offerId': 'o1', 'remaining': 60,
             'state': 'ACTIVE', 'effectivePricePerEnergy': '12.50',
             'pricePerEnergy': '11.0', 'routeEnabled': True,
             'lastEventAt': iso(-5), 'jwt': 'MUST_NOT_LEAK'},
            {'cityId': 'HGW', 'offerId': 'o2', 'remaining': 40,
             'state': 'ACTIVE', 'effectivePricePerEnergy': '13.50',
             'routeEnabled': False, 'lastEventAt': iso(-5)},
            {'cityId': 'MET', 'offerId': 'o3', 'remaining': 999,
             'state': 'WITHDRAWN', 'routeEnabled': False,
             'effectivePricePerEnergy': '1.00'},
        ],
    }


def evidence(phase='final', metrics_override=None):
    return make_evidence(slot_row=slot(), phase=phase, history=history(),
                         metrics=metrics_override if metrics_override is not None else metrics(),
                         book=book(), collected_at=iso(30 if phase == 'early' else 360), environment='production',
                         commit='deadbeef', notes='Mercado fluctuante')


class CaptureTests(unittest.TestCase):
    def test_capture_is_only_get_projection_without_secrets(self):
        seen = []
        def fetch(path):
            seen.append(path)
            if path.startswith('/cycles/'):
                return history()
            if path.startswith('/market/cycle-metrics'):
                return metrics()
            return book()
        out = capture(fetch, slot_row=slot(), phase='final',
                      environment='production', commit='deadbeef')
        self.assertEqual(len(seen), 3)
        self.assertTrue(all(p.startswith('/') for p in seen))
        self.assertEqual(out['source'], 'REAL_API_READONLY')
        self.assertEqual(out['metrics']['spanSeconds'], int(slot()['spanSeconds']))
        serialized = json.dumps(out)
        self.assertNotIn('MUST_NOT_LEAK', serialized)
        self.assertNotIn('swapToken', serialized)
        self.assertNotIn('password', serialized)
        self.assertNotIn('jwt', serialized)

    def test_excludes_unconfirmed_metric_fields(self):
        out = make_evidence(slot_row=slot(), phase='early', history=history(),
                            metrics=None, book=book(), collected_at=iso(),
                            environment='local', commit='unknown')
        self.assertIsNone(out['metrics'])
        self.assertIn('METRICS_NOT_READY', out['qualityFlags'])
        self.assertIn('DEPLOYMENT_COMMIT_NOT_RECORDED', out['qualityFlags'])

    def test_mismatch_of_cycle_id_is_detected(self):
        wrong = metrics()
        wrong['cycleId'] = 'another'
        with self.assertRaisesRegex(ValueError, 'otro cycleId'):
            make_evidence(slot_row=slot(), phase='final', history=history(),
                          metrics=wrong, book=book(), collected_at=iso(),
                          environment='local', commit='deadbeef')

    def test_deterministic_event_timeline_no_body_inclusion(self):
        e = safe_events(history())
        self.assertEqual(len(e), 9)
        self.assertEqual([item['eventType'] for item in e if item['eventType'] == 'bid-responded'],
                         ['bid-responded'])
        self.assertEqual(e, sorted(e, key=lambda x: (x['at'], x['eventType'], x['ref'])))
        self.assertNotIn('MUST_NOT_LEAK', json.dumps(e))

    def test_external_prices_liquidity_and_disabled_routes(self):
        e = external_factors(book(), history(), metrics())
        self.assertEqual(e['activeOffersInSnapshot'], 2)
        self.assertEqual(e['liquidityRatioAtCapture'], '1')
        self.assertEqual(e['medianEffectivePriceAtCapture'], '13.00')
        self.assertEqual(e['disabledRoutesShareAtCapture'], '0.5')
        self.assertEqual(e['marketWindowSeconds'], 360)
        self.assertFalse(e['bookSnapshotIsHistoricalTimeline'])

    def test_save_json_no_unintentional_overwrite(self):
        with TemporaryDirectory() as d:
            path = Path(d) / 'cycle.json'
            save_json(path, evidence())
            with self.assertRaises(FileExistsError):
                save_json(path, evidence())
            save_json(path, evidence('early'), replace=True)
            self.assertEqual(read_json(path)['phase'], 'early')

    def test_missing_market_book_does_not_fake_prices(self):
        data = make_evidence(slot_row=slot(), phase='final', history=history(),
                             metrics=metrics(), book=None, collected_at=iso(),
                             environment='production', commit='deadbeef')
        self.assertIn('BOOK_UNAVAILABLE', data['qualityFlags'])
        self.assertIsNone(data['externalFactors']['activeOffersInSnapshot'])
        self.assertIsNone(data['externalFactors']['medianEffectivePriceAtCapture'])


class WalletTests(unittest.TestCase):
    def test_parses_docker_structured_logs_with_safe_fields(self):
        with TemporaryDirectory() as d:
            log = Path(d) / 'wallet.log'
            log.write_text(
                f'{iso(10)} master-1 | INFO wallet_http method=POST endpoint=swaps_accept '
                'outcome=http_202 latencyMs=39.25 idpk=known-uuid-key header=SECRET\n'
                'ignore_this_line fake=foo\n'
                f'{iso(15)} master-1 | INFO wallet_http method=GET endpoint=balance '
                'outcome=http_200 latencyMs=8.50\n', encoding='utf-8')
            items, invalid = read_wallet_observations(log)
            self.assertEqual(len(items), 2)
            self.assertEqual(invalid, 0)
            self.assertNotIn('header', json.dumps(items))
            self.assertEqual(items[0]['idpk'], 'known-uuid-key')

    def test_strict_correlation_rejects_unknown_or_missing_idpk(self):
        observations = [
            {'endpoint': 'swaps_accept', 'latencyMs': 10, 'outcome': 'http_202',
             'idpk': 'known-uuid-key', 'cycleId': '', 'timestamp': iso()},
            {'endpoint': 'balance', 'latencyMs': 20, 'outcome': 'http_200',
             'idpk': '', 'cycleId': '', 'timestamp': iso()},
            {'endpoint': 'swaps_propose', 'latencyMs': 5, 'outcome': 'http_202',
             'idpk': 'another-uuid', 'cycleId': 'different', 'timestamp': iso()},
        ]
        result = aggregate_wallet(observations, evidence=evidence())
        self.assertEqual(result['attributedObservations'], 1)
        self.assertEqual(result['attribution'], {'KNOWN_ID_MATCH': 1})
        self.assertEqual(result['excludedCounts']['NOT_ATTRIBUTABLE'], 1)
        self.assertEqual(result['excludedCounts']['OTHER_CYCLE_ID'], 1)

    def test_manual_attribution_is_explicitly_labeled(self):
        obs = [{'endpoint': 'balance', 'latencyMs': 23, 'outcome': 'timeout',
                'idpk': '', 'cycleId': '', 'timestamp': iso()}]
        result = aggregate_wallet(obs, evidence=evidence(), assume_isolated=True)
        self.assertEqual(result['attribution'], {'ASSUMED_ISOLATED': 1})
        self.assertEqual(result['timeoutCount'], 1)

    def test_nonmatching_direct_cycle_id_cannot_be_force_assigned(self):
        obs = [{'endpoint': 'balance', 'latencyMs': 23, 'outcome': 'http_200',
                'idpk': '', 'cycleId': 'different', 'timestamp': iso()}]
        result = aggregate_wallet(obs, evidence=evidence(), assume_isolated=True)
        self.assertEqual(result['attributedObservations'], 0)

    def test_time_window_excludes_observations_even_if_ids_match(self):
        obs = [{'endpoint': 'swaps_accept', 'latencyMs': 23, 'outcome': 'http_200',
                'idpk': 'known-uuid-key', 'cycleId': '', 'timestamp': iso(-5000)}]
        result = aggregate_wallet(obs, evidence=evidence())
        self.assertEqual(result['excludedCounts'], {'OUTSIDE_CYCLE_WINDOW': 1})

    def test_percentiles_are_reproducible(self):
        self.assertEqual(percentile([10, 20, 30, 40], 50), 25)
        self.assertEqual(percentile([10, 20, 30, 40], 95), 38.5)
        self.assertIsNone(percentile([], 95))

    def test_parses_csv_export(self):
        with TemporaryDirectory() as d:
            path = Path(d) / 'wallet.csv'
            path.write_text('cycleId,endpoint,latencyMs,outcome,timestamp\n'
                            f'cycle-real-001,balance,5.25,http_200,{iso()}\n'
                            f'cycle-real-001,balance,-4,http_200,{iso()}\n', encoding='utf-8')
            observations, invalid = read_wallet_observations(path)
            self.assertEqual(len(observations), 1)
            self.assertEqual(invalid, 1)


class ReportTests(unittest.TestCase):
    def test_empty_plan_outputs_pending_not_fake_results(self):
        with TemporaryDirectory() as d:
            plan = build_plan(blocks=1)
            rows, events, spans, report = build_report(plan, Path(d))
            self.assertEqual(len(rows), 5)
            self.assertEqual(events, [])
            self.assertEqual(sum(s['economicallyComparable'] for s in spans), 0)
            self.assertIn('SLOT_NOT_EXECUTED', report)
            self.assertEqual(rows[0]['realCost'], '')
            self.assertEqual(rows[0]['eventCount'], '')  # ausente, no cero inventado

    def test_full_evidence_and_wallet_produce_comparable_row(self):
        with TemporaryDirectory() as d:
            base = Path(d)
            plan = [slot()]
            save_json(evidence_path(base, 1, plan[0]['cycleId'], 'early'), evidence('early'))
            save_json(evidence_path(base, 1, plan[0]['cycleId'], 'final'), evidence('final'))
            w = aggregate_wallet([{
                'endpoint': 'swaps_accept', 'latencyMs': 37, 'outcome': 'http_202',
                'idpk': 'known-uuid-key', 'cycleId': '', 'timestamp': iso()}],
                evidence=evidence())
            save_json(wallet_path(base, plan[0]['cycleId']), w)
            rows, events, spans, report = build_report(plan, base)
            self.assertEqual(rows[0]['readyForEconomicComparison'], 'YES')
            self.assertEqual(rows[0]['walletExact'], '1')
            self.assertEqual(rows[0]['eventCount'], '9')
            self.assertEqual(len(events), 9)
            self.assertEqual(spans[0]['economicallyComparable'], 1)
            self.assertEqual(spans[0]['withWalletLatency'], 1)
            self.assertIn('observacional', report)

    def test_early_events_preserved_when_final_is_not_available(self):
        with TemporaryDirectory() as d:
            base = Path(d)
            row = slot()
            save_json(evidence_path(base, 1, row['cycleId'], 'early'), evidence('early'))
            rows, events, spans, _ = build_report([row], base)
            self.assertEqual(len(events), 9)
            self.assertEqual(rows[0]['eventCount'], '')
            self.assertEqual(rows[0]['readyForEconomicComparison'], 'NO')
            self.assertIn('FINAL_SNAPSHOT_MISSING', rows[0]['qualityFlags'])

    def test_ad6_mismatch_blocks_economic_comparison(self):
        with TemporaryDirectory() as d:
            base = Path(d)
            plan = [slot()]
            altered = metrics()
            altered['spanSeconds'] += 30
            save_json(evidence_path(base, 1, plan[0]['cycleId'], 'final'),
                      evidence('final', metrics_override=altered))
            rows, _, _, _ = build_report(plan, base)
            self.assertEqual(rows[0]['readyForEconomicComparison'], 'NO')
            self.assertIn('SPAN_MISMATCH_AD6', rows[0]['qualityFlags'])

    def test_evidence_from_another_cycle_is_rejected(self):
        with TemporaryDirectory() as d:
            base = Path(d)
            plan = [slot()]
            wrong = evidence('final')
            wrong['cycleId'] = 'other'
            save_json(evidence_path(base, 1, plan[0]['cycleId'], 'final'), wrong)
            with self.assertRaisesRegex(ValueError, 'incompatible'):
                build_report(plan, base)


if __name__ == '__main__':
    unittest.main()
