"""Reintentos HTTP deterministas sin Wallet, AWS ni esperas reales."""
import io
import json
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_wallet_lambda import Clock, FakeQuota, FakeTransport, response
import errors
import handler
import wallet_client as wallet
from test_e3046_buyer_acceptance import FakeStore, EVENT, NOW


class WalletRetriesTest(unittest.TestCase):
    def client(self, responses, *, uniform=lambda low, high: high, attempts=4):
        self.clock = Clock()
        self.quota = FakeQuota()
        self.transport = FakeTransport(responses)
        return wallet.WalletClient(
            wallet.WalletConfig('https://wallet.invalid/v1', 'fake', 5, attempts),
            self.quota, transport=self.transport, sleep=self.clock.sleep,
            monotonic=self.clock.monotonic, uniform=uniform,
        )

    def accept(self, client, deadline=20):
        return client.accept(swap_id='one-swap', idempotency_key='one-operation',
                             deadline_seconds=deadline)

    def test_retry_after_precedes_exponential_delay_including_zero(self):
        for wait in (0, 1, 9):
            with self.subTest(wait=wait):
                client = self.client([response(502), response(503, headers={'retry-after': str(wait)}),
                                      response(202)])
                self.accept(client)
                self.assertEqual(self.clock.sleeps, [1, wait])
                self.assertEqual(self.quota.blocks, [('wallet:write', wait)])

    def test_missing_or_invalid_retry_after_uses_bounded_jitter(self):
        for value in ('invalid', '-1', '1.5', ''):
            client = self.client([response(429, headers={'Retry-After': value}), response(202)],
                                 uniform=lambda low, high: low)
            self.accept(client)
            self.assertEqual(self.clock.sleeps, [0.5])

    def test_budget_stops_after_four_attempts_without_final_sleep(self):
        client = self.client([response(500)] * 4, uniform=lambda low, high: low)
        with self.assertRaises(errors.WalletRetryable):
            self.accept(client)
        self.assertEqual(len(self.transport.requests), 4)
        self.assertEqual(self.clock.sleeps, [0.5, 1, 2])
        self.assertEqual(client._backoff(50), 4)  # mitad del tope de 8 s
        with self.assertRaises(ValueError):
            wallet.WalletConfig('https://wallet.invalid', 'fake', max_attempts=5)

    def test_timeout_after_remote_commit_replays_same_body_and_identity(self):
        client = self.client([])
        accepted = {}
        requests = []

        def request(method, url, *, headers, body, timeout):
            identity = headers['Idempotency-Key']
            requests.append((identity, body))
            if identity not in accepted:
                accepted[identity] = json.loads(body)
                raise TimeoutError('response lost after commit')
            self.assertEqual(accepted[identity], json.loads(body))
            return response(200, {'swapId': 'one-swap', 'status': 'proposed'})

        self.transport.request = request
        result = client.propose(idempotency_key='one-operation',
                                swap={'memo': 'KLD-cycle', 'expiresAt': 'fixed'},
                                deadline_seconds=20)
        self.assertEqual(result.data['swapId'], 'one-swap')
        self.assertEqual(len(accepted), 1)
        self.assertEqual(requests[0], requests[1])

    def test_all_422_are_terminal_and_official_reason_is_recognized(self):
        for reason, expected in [('IDEMPOTENCY_MISMATCH', errors.WalletIdempotencyMismatch),
                                 ('MALFORMED_REQUEST', errors.WalletRejected),
                                 ('UNKNOWN', errors.WalletRejected)]:
            client = self.client([response(422, {'reason': reason, 'code': 422})])
            with self.assertRaises(expected):
                self.accept(client)
            self.assertEqual(len(self.transport.requests), 1)
            self.assertEqual(self.clock.sleeps, [])

    def test_html_gateway_error_keeps_retry_policy(self):
        transport = wallet.UrllibTransport()
        error = HTTPError('https://wallet.invalid', 502, 'bad gateway', {},
                          io.BytesIO(b'<html>Bad gateway</html>'))
        with patch.object(wallet, 'urlopen', side_effect=error):
            result = transport.request('POST', 'https://wallet.invalid',
                                       headers={}, body=None, timeout=1)
        self.assertEqual(result.status_code, 502)
        self.assertEqual(result.data, {})

    def test_quota_wait_that_consumes_deadline_never_sends_http(self):
        client = self.client([])
        def reserve(category):
            self.clock.value = 10
            return 0
        self.quota.reserve = reserve
        with self.assertRaises(errors.WalletRetryable):
            self.accept(client, deadline=5)
        self.assertEqual(self.transport.requests, [])

    def test_sleep_overshoot_cannot_start_another_http(self):
        client = self.client([response(503, headers={'Retry-After': '1'})])
        client.sleep = lambda seconds: setattr(self.clock, 'value', 30)
        with self.assertRaises(errors.WalletRetryable):
            self.accept(client)
        self.assertEqual(len(self.transport.requests), 1)

    def test_contention_rechecks_quota_until_slot_or_deadline(self):
        client = self.client([response(202)])
        with patch.object(self.quota, 'reserve', side_effect=[2, 2, 0]):
            self.accept(client)
        self.assertEqual(self.clock.sleeps, [2, 2])
        self.assertEqual(len(self.transport.requests), 1)

    def test_subsecond_deadline_is_not_increased_by_transport(self):
        client = self.client([response(202)])
        self.accept(client, deadline=0.02)
        self.assertEqual(self.transport.requests[0][4], 0.02)

    def test_buyer_retries_end_at_persisted_expiry(self):
        store = FakeStore()
        client = self.client([response(503, headers={'Retry-After': '6'})])
        with patch.object(handler, 'verify_buyer_acceptance_identity',
                          return_value={'deadlineAt': NOW + timedelta(seconds=5)}), \
             patch.object(handler, 'prepare_buyer_acceptance', return_value={'action': 'POST'}), \
             patch.object(handler, 'complete_buyer_acceptance_attempt'):
            with self.assertRaises(errors.WalletRateLimited):
                handler._callback_wallet_task('buyer-acceptance', EVENT, object(), store, client)
        self.assertEqual(len(self.transport.requests), 1)
        self.assertEqual(self.clock.sleeps, [])

    def test_seller_retries_end_at_frozen_expiry(self):
        store = FakeStore()
        client = self.client([response(429, headers={'Retry-After': '6'})])
        event = dict(EVENT, operation='PROPOSE', swap={})
        with patch.object(handler, 'prepare_seller_proposal',
                          return_value={'expiresAt': (NOW + timedelta(seconds=5)).isoformat()}):
            with self.assertRaises(errors.WalletRateLimited):
                handler._callback_wallet_task('seller-proposal', event, object(), store, client)
        self.assertEqual(len(self.transport.requests), 1)

    def test_lost_proposal_response_resumes_with_official_replay_and_frozen_expiry(self):
        store = FakeStore()
        expiry = (NOW + timedelta(seconds=25)).isoformat()
        client = self.client([TimeoutError(), response(200, {
            'swapId': 'one-swap', 'status': 'proposed',
            'lastEvent': {'type': 'swap-proposed', 'data': {'expiresAt': expiry}},
        })])
        with patch.object(handler, 'prepare_seller_proposal',
                          return_value={'expiresAt': expiry}), \
             patch.object(handler, '_complete_known_callback') as callback:
            handler._callback_wallet_task('seller-proposal',
                                          dict(EVENT, operation='PROPOSE', swap={}),
                                          object(), store, client)
        self.assertEqual(callback.call_args.kwargs['outcome'], {
            'outcome': 'PROPOSED', 'swapId': 'one-swap', 'expiresAt': expiry,
        })
        self.assertEqual(self.transport.requests[0][2:4], self.transport.requests[1][2:4])

    def test_step_functions_retry_budget_is_bounded_and_excludes_422(self):
        directory = Path(__file__).resolve().parents[1] / 'energyshark_infra' / 'asl'
        for name in ('seller', 'buyer'):
            states = json.loads((directory / (name + '.asl.json')).read_text())['States']
            for state in states.values():
                for retry in state.get('Retry', []):
                    if 'WalletRetryable' in retry['ErrorEquals']:
                        self.assertEqual(retry['MaxAttempts'], 4)
                        self.assertEqual(retry['MaxDelaySeconds'], 8)
                        self.assertEqual(retry['JitterStrategy'], 'FULL')
                        self.assertNotIn('WalletRejected', retry['ErrorEquals'])
                        self.assertNotIn('WalletIdempotencyMismatch', retry['ErrorEquals'])
