"""E3-046: identidad, persistencia antes de HTTP y callback sólo por Wallet."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / 'lambda_src' / 'saga_task'
sys.path.insert(0, str(SOURCE))

from buyer_acceptance import (  # noqa: E402
    BuyerAcceptanceInconsistent, BuyerAcceptanceDeadline,
    verify_buyer_acceptance_identity, prepare_buyer_acceptance,
    complete_buyer_acceptance_attempt,
)
import handler  # noqa: E402
from wallet_client import WalletResult  # noqa: E402


NOW = datetime(2026, 11, 23, 14, tzinfo=timezone.utc)
KEY = 'accept-idpk'
SWAP = 'swp-123'


def economic_row(*, record_status='SUBMITTED', saga_status='RUNNING',
                 attempts=0, error=None, retry_at=None, closes_at=None,
                 expires_at=None):
    return (
        'BUYER', saga_status, 'cycle-1', SWAP,
        KEY, 'ACCEPTOR', SWAP, 'cycle-1', record_status, KEY,
        'bid-1', expires_at or NOW + timedelta(minutes=3),
        'CR', Decimal('18.00'), 'KWH', Decimal('3'),
        'secret-jwt-never-export', attempts, error, retry_at,
        'OUTGOING', 'ACCEPTED', 'cycle-1', SWAP,
        'HGW', 'HGW', 3, closes_at or NOW + timedelta(minutes=5),
    )


class FakeCursor:
    def __init__(self, store):
        self.store = store
        self.rowcount = 0

    def __enter__(self): return self
    def __exit__(self, *args): return False

    def execute(self, sql, params):
        self.store.sql.append(sql)
        if sql.lstrip().startswith('SELECT'):
            self.rowcount = 1
        elif "SET status = 'ACCEPT_SUBMITTED'" in sql:
            self.store.row = list(self.store.row)
            self.store.row[8] = 'ACCEPT_SUBMITTED'
            self.store.row[17] += 1
            self.store.row[18] = 'WALLET_IN_FLIGHT'
            self.store.row[19] = params[1]
            self.rowcount = 1
        elif 'SET accept_error =' in sql:
            self.store.row = list(self.store.row)
            self.store.row[18] = params[0]
            self.store.row[19] = params[1]
            self.rowcount = 1
        else:
            raise AssertionError(sql)

    def fetchone(self):
        return tuple(self.store.row) if self.store.row is not None else None


class FakeConnection:
    def __init__(self, store): self.store = store
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def cursor(self): return FakeCursor(self.store)
    def commit(self): self.store.commits += 1


class FakeStore:
    dsn = 'fake-dsn'
    def __init__(self, row=None):
        self.row = economic_row() if row is None else row
        self.sql = []
        self.commits = 0
        self.tokens = []
    def connect(self, dsn):
        assert dsn == self.dsn
        return FakeConnection(self)
    def now(self): return NOW
    def register_task_token(self, **kwargs):
        self.tokens.append(kwargs)
        return {'status': 'WAITING', 'created': True, 'id': 1}


IDENTITY = dict(saga_execution_id=KEY, swap_record_id=11,
                swap_id=SWAP, idempotency_key=KEY)
EVENT = dict(sagaExecutionId=KEY, swapRecordId=11, swapId=SWAP,
             idempotencyKey=KEY, correlationIdpk=KEY,
             taskToken='opaque', timeoutAt='2026-11-23T14:05:00Z',
             operation='ACCEPT')


class BuyerAcceptanceTests(unittest.TestCase):
    def test_valid_identity_never_returns_secret(self):
        store = FakeStore()
        result = verify_buyer_acceptance_identity(store, **IDENTITY)
        self.assertEqual(result['status'], 'SUBMITTED')
        self.assertNotIn('secret-jwt', str(result))
        self.assertEqual(store.commits, 0)

    def test_invalid_correlation_fails_without_write(self):
        for changes in (dict(saga_execution_id='other'),
                        dict(swap_id='other'),
                        dict(idempotency_key='different'),
                        dict(swap_record_id=0)):
            with self.subTest(changes=changes):
                store = FakeStore()
                with self.assertRaises(BuyerAcceptanceInconsistent):
                    verify_buyer_acceptance_identity(store, **(IDENTITY | changes))
                self.assertEqual(store.commits, 0)

    def test_persists_submitted_before_http_and_reuses_key(self):
        store = FakeStore()
        result = prepare_buyer_acceptance(store, **IDENTITY)
        self.assertEqual(result, {'action': 'POST', 'attempt': 1})
        self.assertEqual(store.row[8], 'ACCEPT_SUBMITTED')
        self.assertEqual(store.row[17], 1)
        self.assertEqual(store.row[18], 'WALLET_IN_FLIGHT')
        self.assertEqual(store.commits, 1)
        self.assertTrue(any('FOR UPDATE OF sr' in sql for sql in store.sql))
        with self.assertRaisesRegex(Exception, 'en vuelo'):
            prepare_buyer_acceptance(store, **IDENTITY)
        self.assertEqual(store.row[17], 1)

    def test_http_202_does_not_settle_or_duplicate(self):
        store = FakeStore()
        prepare_buyer_acceptance(store, **IDENTITY)
        complete_buyer_acceptance_attempt(store, **IDENTITY, status_code=202)
        self.assertEqual(store.row[8], 'ACCEPT_SUBMITTED')
        self.assertIsNone(store.row[18])
        self.assertEqual(prepare_buyer_acceptance(store, **IDENTITY)['action'], 'AWAIT_EVENT')
        self.assertEqual(store.row[17], 1)

    def test_http_200_terminal_does_not_settle_without_wallet_event(self):
        store = FakeStore()
        prepare_buyer_acceptance(store, **IDENTITY)
        complete_buyer_acceptance_attempt(store, **IDENTITY,
                                          status_code=200, wallet_status='SETTLED')
        self.assertEqual(store.row[8], 'ACCEPT_SUBMITTED')
        self.assertEqual(store.row[18], 'WALLET_TERMINAL_PENDING_EVENT')

    def test_late_result_does_not_overwrite_terminal_event(self):
        store = FakeStore()
        prepare_buyer_acceptance(store, **IDENTITY)
        store.row[8] = 'SETTLED'
        result = complete_buyer_acceptance_attempt(store, **IDENTITY, status_code=202)
        self.assertTrue(result['eventWonRace'])
        self.assertEqual(store.row[8], 'SETTLED')

    def test_deadline_prevents_wallet_attempt(self):
        store = FakeStore(economic_row(expires_at=NOW + timedelta(seconds=2)))
        with self.assertRaises(BuyerAcceptanceDeadline):
            prepare_buyer_acceptance(store, **IDENTITY)
        self.assertEqual(store.commits, 0)

    def test_202_handler_no_callback_without_event(self):
        store = FakeStore()
        order = []
        class Wallet:
            def accept(self, **kwargs):
                assert order == ['prepared']  # intención antes del HTTP
                assert store.tokens
                assert kwargs['idempotency_key'] == KEY
                return WalletResult(202, {'status': 'accepted'}, True, KEY)
        with patch.object(handler, 'verify_buyer_acceptance_identity',
                          side_effect=lambda *_args, **_kwargs: {'status': 'SUBMITTED', 'deadlineAt': NOW + timedelta(seconds=25)}), \
             patch.object(handler, 'prepare_buyer_acceptance',
                          side_effect=lambda *_args, **_kwargs: (order.append('prepared') or {'action': 'POST'})), \
             patch.object(handler, 'complete_buyer_acceptance_attempt') as complete, \
             patch.object(handler, '_complete_known_callback') as callback:
            result = handler._callback_wallet_task('buyer-acceptance', EVENT,
                                                   object(), store, Wallet())
        self.assertTrue(result['awaitingWalletEvent'])
        self.assertNotIn('wallet', result)
        callback.assert_not_called()
        complete.assert_called_once()

    def test_200_handler_does_not_complete_without_persisted_event(self):
        store = FakeStore()
        class Wallet:
            def accept(self, **kwargs):
                return WalletResult(200, {'status': 'settled', 'swapId': SWAP}, False, KEY)
        with patch.object(handler, 'verify_buyer_acceptance_identity',
                          return_value={'deadlineAt': NOW + timedelta(seconds=25)}), \
             patch.object(handler, 'prepare_buyer_acceptance', return_value={'action': 'POST'}), \
             patch.object(handler, 'complete_buyer_acceptance_attempt'), \
             patch.object(handler, '_complete_known_callback') as callback:
            result = handler._callback_wallet_task('buyer-acceptance', EVENT,
                                                   object(), store, Wallet())
        self.assertTrue(result['awaitingWalletEvent'])
        callback.assert_not_called()

    def test_handler_rejects_identity_before_task_token_replacement(self):
        store = FakeStore()
        with patch.object(handler, 'verify_buyer_acceptance_identity',
                          side_effect=BuyerAcceptanceInconsistent('bad')):
            with self.assertRaises(BuyerAcceptanceInconsistent):
                handler._callback_wallet_task('buyer-acceptance', EVENT,
                                              object(), store, object())
        self.assertEqual(store.tokens, [])


if __name__ == '__main__':
    unittest.main()
