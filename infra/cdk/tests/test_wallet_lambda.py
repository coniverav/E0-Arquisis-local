import importlib
import sys
import unittest
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1] / "lambda_src" / "saga_task"
sys.path.insert(0, str(SOURCE))

errors = importlib.import_module("errors")
handler = importlib.import_module("handler")
wallet_module = importlib.import_module("wallet_client")


class FakeQuota:
    def __init__(self):
        self.reservations = []
        self.blocks = []

    def reserve(self, category):
        self.reservations.append(category)
        return 0

    def block(self, category, seconds):
        self.blocks.append((category, seconds))


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, method, url, *, headers, body, timeout):
        self.requests.append((method, url, dict(headers), body, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class Clock:
    def __init__(self):
        self.value = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += seconds


def response(status, data=None, headers=None):
    return wallet_module.HttpResponse(status, headers or {}, data or {})


class WalletClientTest(unittest.TestCase):
    def client(self, responses, *, max_attempts=4):
        quota = FakeQuota()
        transport = FakeTransport(responses)
        clock = Clock()
        client = wallet_module.WalletClient(
            wallet_module.WalletConfig(
                "https://wallet.example/v1", "secret-key", 5, max_attempts
            ),
            quota,
            transport=transport,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )
        return client, quota, transport, clock

    def test_202_uses_secret_and_stable_idempotency_key_without_settling(self):
        client, quota, transport, _ = self.client([
            response(202, {"swapId": "swap-1", "status": "accepted"})
        ])
        result = client.propose(
            idempotency_key="stable-operation",
            swap={"give": {"asset": "KWH", "amount": "1"}},
            deadline_seconds=20,
        )
        self.assertTrue(result.accepted_for_processing)
        self.assertEqual(result.status_code, 202)
        self.assertEqual(quota.reservations, ["wallet:write"])
        headers = transport.requests[0][2]
        self.assertEqual(headers["X-Api-Key"], "secret-key")
        self.assertEqual(headers["Idempotency-Key"], "stable-operation")

    def test_429_respects_retry_after_and_reuses_same_key(self):
        client, quota, transport, clock = self.client([
            response(429, {"code": "RATE_LIMIT"}, {"Retry-After": "3"}),
            response(200, {"swapId": "swap-1", "status": "proposed"}),
        ])
        result = client.accept(
            swap_id="swap-1",
            idempotency_key="accept-key",
            deadline_seconds=20,
        )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(clock.sleeps, [3])
        self.assertEqual(quota.blocks, [("wallet:write", 3)])
        self.assertEqual(
            [request[2]["Idempotency-Key"] for request in transport.requests],
            ["accept-key", "accept-key"],
        )

    def test_5xx_and_timeout_backoff_without_new_identity(self):
        client, _, transport, clock = self.client([
            TimeoutError(),
            response(500),
            response(202, {"swapId": "swap-2", "status": "accepted"}),
        ])
        result = client.cancel(
            swap_id="swap-2",
            idempotency_key="cancel-key",
            deadline_seconds=20,
        )
        self.assertEqual(result.status_code, 202)
        self.assertEqual(clock.sleeps, [1, 2])
        self.assertEqual(
            [request[2]["Idempotency-Key"] for request in transport.requests],
            ["cancel-key", "cancel-key", "cancel-key"],
        )

    def test_503_honors_retry_after(self):
        client, quota, _, clock = self.client([
            response(503, {}, {"Retry-After": "4"}),
            response(200, {"swapId": "swap-3", "status": "settled"}),
        ])
        result = client.get_swap(swap_id="swap-3", deadline_seconds=20)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(clock.sleeps, [4])
        self.assertEqual(quota.blocks, [("wallet:read", 4)])

    def test_permanent_errors_are_not_retried(self):
        cases = (
            (401, {}, errors.WalletUnauthorized),
            (404, {}, errors.WalletNotFound),
            (422, {"code": "IDEMPOTENCY_MISMATCH"}, errors.WalletIdempotencyMismatch),
        )
        for status, data, expected in cases:
            with self.subTest(status=status):
                client, _, transport, _ = self.client([response(status, data)])
                with self.assertRaises(expected):
                    client.accept(
                        swap_id="swap-1",
                        idempotency_key="same-key",
                        deadline_seconds=20,
                    )
                self.assertEqual(len(transport.requests), 1)

    def test_http_200_exposes_authoritative_state_but_202_does_not(self):
        known = handler._known_outcome(wallet_module.WalletResult(
            200,
            {"swapId": "swap-4", "status": "settled"},
            False,
            "same-key",
        ))
        self.assertEqual(known, {"outcome": "SETTLED", "swapId": "swap-4"})
        self.assertIsNone(handler._known_outcome(wallet_module.WalletResult(
            202,
            {"swapId": "swap-4", "status": "accepted"},
            True,
            "same-key",
        )))


class TokenBeforeWalletTest(unittest.TestCase):
    def test_token_is_visible_before_wallet_and_fast_event_wins_race(self):
        from unittest.mock import patch

        order = []

        class Store:
            status = "WAITING"

            def register_task_token(self, **kwargs):
                order.append("token-committed")
                return {"id": 1, "status": self.status, "created": True}

        store = Store()

        class Wallet:
            def propose(self, **kwargs):
                self.assertion = order == ["token-committed", "proposal-frozen"]
                order.append("event-correlated")
                store.status = "SUCCEEDED"
                order.append("http-response")
                return wallet_module.WalletResult(202, {"status": "accepted"}, True, "key")

        wallet = Wallet()
        event = {
            "taskToken": "opaque-token",
            "sagaExecutionId": "key",
            "correlationIdpk": "key",
            "idempotencyKey": "key",
            "operation": "PROPOSE",
            "swap": {"idpk": "key", "idempotencyKey": "key",
                     "give": {"asset": "KWH", "amount": "1"}},
            "timeoutAt": "2099-01-01T00:00:00Z",
        }
        context = type("Context", (), {"get_remaining_time_in_millis": lambda self: 20000})()
        def freeze_proposal(_store, **kwargs):
            self.assertEqual(order, ["token-committed"])
            self.assertEqual(kwargs["saga_execution_id"], "key")
            self.assertEqual(kwargs["idempotency_key"], "key")
            order.append("proposal-frozen")
            return {"give": {"asset": "KWH", "amount": 1},
                    "take": {"asset": "CR", "amount": 1.0},
                    "memo": "KLD-cycle", "expiresAt": "2099-01-01T00:00:00Z"}

        with patch.object(handler, "prepare_seller_proposal", side_effect=freeze_proposal):
            result = handler._callback_wallet_task(
                "seller-proposal", event, context, store, wallet
            )
        self.assertTrue(wallet.assertion)
        self.assertEqual(order, ["token-committed", "proposal-frozen",
                                 "event-correlated", "http-response"])
        self.assertTrue(result["acceptedForProcessing"])

    def test_completed_token_replay_does_not_call_wallet_again(self):
        class Store:
            def register_task_token(self, **kwargs):
                return {"id": 1, "status": "SUCCEEDED", "created": False}

        class Wallet:
            def propose(self, **kwargs):
                raise AssertionError("No debe repetir la operación económica")

        event = {
            "taskToken": "opaque-token",
            "sagaExecutionId": 1,
            "correlationIdpk": "key",
            "idempotencyKey": "key",
            "operation": "PROPOSE",
            "swap": {},
            "timeoutAt": "2099-01-01T00:00:00Z",
        }
        result = handler._callback_wallet_task(
            "seller-proposal", event, object(), Store(), Wallet()
        )
        self.assertEqual(result, {"callbackAlreadyCompleted": True})


if __name__ == "__main__":
    unittest.main()
