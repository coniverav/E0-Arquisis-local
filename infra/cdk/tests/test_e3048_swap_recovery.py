"""E3-048: GET y cancel con identidad comprobada, sin AWS ni Wallet real."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

SOURCE = Path(__file__).resolve().parents[1] / "lambda_src" / "saga_task"
sys.path.insert(0, str(SOURCE))

from swap_recovery import (  # noqa: E402
    SagaRecoveryInconsistent, SagaRecoveryPending,
    cancel_key_for_seller, reconcile_swap, cancel_seller_and_recheck,
)


class Cursor:
    def __init__(self, store):
        self.store = store
    def __enter__(self):
        return self
    def __exit__(self, *_):
        pass
    def execute(self, sql, params):
        self.store.queries.append((sql, params))
    def fetchone(self):
        return self.store.row


class Conn:
    def __init__(self, store):
        self.store = store
    def __enter__(self):
        return self
    def __exit__(self, *_):
        pass
    def cursor(self):
        return Cursor(self.store)


class Store:
    dsn = "not-used"
    def __init__(self, row):
        self.row = row
        self.queries = []
    def connect(self, _dsn):
        return Conn(self)


class Wallet:
    def __init__(self, *statuses):
        self.statuses = list(statuses)
        self.calls = []
    def get_swap(self, *, swap_id, deadline_seconds):
        self.calls.append(("GET", swap_id))
        data = self.statuses.pop(0)
        if isinstance(data, Exception):
            raise data
        if isinstance(data, str):
            data = {"swapId": swap_id, "status": data,
                    "expiresAt": "2099-01-01T00:00:00Z",
                    "lastEvent": {"token": "DO-NOT-EXPOSE"}}
        return SimpleNamespace(status_code=200, data=data)
    def cancel(self, *, swap_id, idempotency_key, deadline_seconds):
        self.calls.append(("POST", swap_id, idempotency_key))
        return SimpleNamespace(status_code=202, data={})


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.saga = str(uuid4())
        self.swap_id = "swp-" + uuid4().hex
        self.store = Store((
            "SELLER", "cycle", self.swap_id, "RUNNING",
            self.saga, "cycle", "PROPOSER", self.swap_id,
            self.saga, None, datetime(2099, 1, 1, tzinfo=timezone.utc),
        ))
        self.time = lambda: 20.0

    def test_get_correlated_and_sanitized(self):
        wallet = Wallet("PROPOSED")
        got = reconcile_swap(self.store, wallet, saga_execution_id=self.saga,
                             role="SELLER", swap_id=self.swap_id,
                             idempotency_key=self.saga, remaining_seconds=self.time)
        self.assertEqual(got["status"], "PROPOSED")
        self.assertNotIn("token", str(got).lower())
        self.assertEqual(wallet.calls, [("GET", self.swap_id)])

    def test_wrong_swap_id_never_uses_network(self):
        wallet = Wallet("SETTLED")
        with self.assertRaises(SagaRecoveryInconsistent):
            reconcile_swap(self.store, wallet, saga_execution_id=self.saga,
                           role="SELLER", swap_id="another", idempotency_key=self.saga,
                           remaining_seconds=self.time)
        self.assertEqual(wallet.calls, [])

    def test_wrong_role_never_uses_network(self):
        wallet = Wallet("SETTLED")
        with self.assertRaises(SagaRecoveryInconsistent):
            reconcile_swap(self.store, wallet, saga_execution_id=self.saga,
                           role="BUYER", swap_id=self.swap_id,
                           idempotency_key=None, remaining_seconds=self.time)
        self.assertFalse(wallet.calls)

    def test_wrong_key_rejected_before_cancel(self):
        wallet = Wallet("PROPOSED")
        with self.assertRaises(SagaRecoveryInconsistent):
            cancel_seller_and_recheck(self.store, wallet, saga_execution_id=self.saga,
                                      swap_id=self.swap_id, idempotency_key="wrong",
                                      remaining_seconds=self.time)
        self.assertFalse(wallet.calls)

    def test_cancel_rechecks_terminal_after_http_202(self):
        wallet = Wallet("PROPOSED", "CANCELLED")
        key = cancel_key_for_seller(self.saga)
        got = cancel_seller_and_recheck(self.store, wallet, saga_execution_id=self.saga,
                                        swap_id=self.swap_id, idempotency_key=key,
                                        remaining_seconds=self.time)
        self.assertEqual(got["status"], "CANCELLED")
        self.assertEqual(wallet.calls, [("GET", self.swap_id),
                                        ("POST", self.swap_id, key),
                                        ("GET", self.swap_id)])

    def test_terminal_before_cancel_makes_no_http_post(self):
        wallet = Wallet("SETTLED")
        got = cancel_seller_and_recheck(self.store, wallet, saga_execution_id=self.saga,
                                        swap_id=self.swap_id,
                                        idempotency_key=cancel_key_for_seller(self.saga),
                                        remaining_seconds=self.time)
        self.assertEqual(got["status"], "SETTLED")
        self.assertEqual(wallet.calls, [("GET", self.swap_id)])

    def test_active_after_cancel_is_ambiguous_not_release(self):
        wallet = Wallet("PROPOSED", "ACCEPTED")
        with self.assertRaises(SagaRecoveryPending):
            cancel_seller_and_recheck(self.store, wallet, saga_execution_id=self.saga,
                                      swap_id=self.swap_id,
                                      idempotency_key=cancel_key_for_seller(self.saga),
                                      remaining_seconds=self.time)
        self.assertEqual([c[0] for c in wallet.calls], ["GET", "POST", "GET"])

    def test_get_wrong_swap_id_raises(self):
        wallet = Wallet({"swapId": "other", "status": "SETTLED"})
        with self.assertRaises(SagaRecoveryInconsistent):
            reconcile_swap(self.store, wallet, saga_execution_id=self.saga,
                           role="SELLER", swap_id=self.swap_id,
                           idempotency_key=self.saga, remaining_seconds=self.time)

    def test_explicit_buyer_role_uses_same_identity(self):
        self.store.row = ("BUYER", "cycle", self.swap_id, "RUNNING",
                          self.saga, "cycle", "ACCEPTOR", self.swap_id,
                          self.saga, self.saga, datetime(2099,1,1,tzinfo=timezone.utc))
        wallet = Wallet("SETTLED")
        got = reconcile_swap(self.store, wallet, saga_execution_id=self.saga,
                             role="BUYER", swap_id=self.swap_id,
                             idempotency_key=None, remaining_seconds=self.time)
        self.assertEqual(got["status"], "SETTLED")

    def test_missing_db_swap_never_fetches(self):
        row = list(self.store.row)
        row[2] = row[7] = None
        self.store.row = tuple(row)
        wallet = Wallet("PROPOSED")
        with self.assertRaises(SagaRecoveryInconsistent):
            reconcile_swap(self.store, wallet, saga_execution_id=self.saga,
                           role="SELLER", swap_id=None,
                           idempotency_key=self.saga, remaining_seconds=self.time)
        self.assertFalse(wallet.calls)


if __name__ == "__main__":
    unittest.main()
