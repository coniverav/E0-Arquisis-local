"""E3-035: casos límite HTTP Wallet, sin conexión externa.

Complementa test_wallet_lambda.py del repositorio. Conserva Idempotency-Key
al reintentar y respeta el tiempo disponible para la tarea.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lambda_src" / "saga_task"))

import errors  # noqa: E402
import wallet_client as wallet  # noqa: E402


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class Quota:
    def __init__(self):
        self.blocks = []

    def reserve(self, category):
        return 0

    def block(self, category, seconds):
        self.blocks.append((category, seconds))


class Transport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, method, url, *, headers, body, timeout):
        self.requests.append((method, url, dict(headers), body, timeout))
        return self.responses.pop(0)


class WalletDeadlineAndRetryTest(unittest.TestCase):
    def build(self, responses):
        transport = Transport(responses)
        clock = Clock()
        quota = Quota()
        client = wallet.WalletClient(
            wallet.WalletConfig("https://wallet.invalid/v3", "test-secret", 5, 4),
            quota,
            transport=transport,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            uniform=lambda low, high: high,
        )
        return client, transport, clock, quota

    def test_429_retry_after_exceeds_deadline_without_second_post(self):
        client, transport, clock, quota = self.build([
            wallet.HttpResponse(429, {"Retry-After": "10"}, {"code": "RATE_LIMIT"}),
        ])
        with self.assertRaises(errors.WalletRateLimited):
            client.accept(swap_id="swap-a", idempotency_key="same-key", deadline_seconds=5)
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(quota.blocks, [("wallet:write", 10)])

    def test_5xx_recovery_uses_same_idempotency_key(self):
        client, transport, clock, _ = self.build([
            wallet.HttpResponse(502, {}, {}),
            wallet.HttpResponse(504, {}, {}),
            wallet.HttpResponse(202, {}, {"swapId": "swap-b"}),
        ])
        result = client.propose(idempotency_key="original-key", swap={"memo": "offer-1"},
                                deadline_seconds=20)
        self.assertEqual(result.status_code, 202)
        self.assertTrue(result.accepted_for_processing)
        self.assertEqual(clock.sleeps, [1, 2])
        self.assertEqual([r[2]["Idempotency-Key"] for r in transport.requests],
                         ["original-key"] * 3)

    def test_422_mismatch_is_fatal_without_retry(self):
        client, transport, _, _ = self.build([
            wallet.HttpResponse(422, {}, {"code": "IDEMPOTENCY_MISMATCH"}),
        ])
        with self.assertRaises(errors.WalletIdempotencyMismatch):
            client.accept(swap_id="swap-c", idempotency_key="original-key", deadline_seconds=20)
        self.assertEqual(len(transport.requests), 1)


if __name__ == "__main__":
    unittest.main()
