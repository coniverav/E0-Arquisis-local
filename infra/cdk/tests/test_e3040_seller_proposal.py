"""E3-040: propuesta seller con contrato Wallet v3 y reintentos seguros.

Pruebas offline: no llaman AWS, Wallet ni PostgreSQL.
"""
import importlib
import sys
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / 'lambda_src' / 'saga_task'
sys.path.insert(0, str(SOURCE))
proposal = importlib.import_module('seller_proposal')
handler = importlib.import_module('handler')
wallet_client = importlib.import_module('wallet_client')

IDPK = '820e9739-a3e6-4a6d-b4f4-7193a47d34ab'
NOW = datetime(2026, 11, 25, 14, 59, tzinfo=timezone.utc)
BODY = {
    'idpk': IDPK,
    'idempotencyKey': IDPK,
    'cancelIdempotencyKey': 'other-stable-key',
    'give': {'asset': 'KWH', 'amount': '40'},
    'take': {'asset': 'CR', 'amount': '170.00'},
    'memo': 'KLD-cycle',
}


class Cursor:
    def __init__(self, *, closes=NOW + timedelta(minutes=4), held='HOLD'):
        self.expires = None
        self.now = NOW
        self.closes = closes
        self.held = held
        self.queries = []
        self.last = ''
        self.rowcount = 0

    def execute(self, statement, args):
        self.last = ' '.join(statement.split())
        self.queries.append((self.last, args))
        if self.last.startswith('UPDATE swap_records'):
            assert self.expires is None
            self.expires = args[0]
            self.rowcount = 1

    def fetchone(self):
        q = self.last
        if q.startswith('SELECT role, cycle_id, status FROM saga_executions'):
            return ('SELLER', 'cycle', 'WAITING')
        if q.startswith('SELECT cycle_id, role, counterpart_city_id, give_asset'):
            return ('cycle', 'PROPOSER', 'COR', 'KWH', Decimal('40'), 'CR',
                    Decimal('170.00'), 'KLD-cycle', 'SUBMITTED', IDPK, self.expires)
        if q.startswith('SELECT cycle_id, offer_id, counterpart_city_id'):
            return ('cycle', 'KLD-cycle', 'COR', 40, 'RESERVED', 'INCOMING')
        if q.startswith('SELECT offer_id, quantity, status FROM offer_reservations'):
            return ('KLD-cycle', 40, self.held)
        if q.startswith('SELECT market_closes_at FROM cycles'):
            return (self.closes,)
        raise AssertionError('Consulta inesperada: '+q)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class Conn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class Store:
    dsn = 'postgresql://fake/test'

    def __init__(self, cursor=None):
        self.cursor_obj = cursor or Cursor()
        self.connection = Conn(self.cursor_obj)
        self.connect = lambda _: self.connection
        self.now = lambda: self.cursor_obj.now


def prepare(store, body=BODY):
    return proposal.prepare_seller_proposal(
        store, saga_execution_id=IDPK,
        idempotency_key=IDPK, swap=body,
    )


class SellerProposalTest(unittest.TestCase):
    def test_stable_body_closed_schema_and_one_freeze(self):
        store = Store()
        first = prepare(store)
        self.assertEqual(set(first), {'give', 'take', 'expiresAt', 'memo'})
        self.assertEqual(first['give'], {'asset': 'KWH', 'amount': 40})
        self.assertEqual(first['take'], {'asset': 'CR', 'amount': 170.0})
        self.assertEqual(first['expiresAt'], '2026-11-25T14:59:55Z')
        store.cursor_obj.now += timedelta(seconds=10)
        self.assertEqual(prepare(store), first)
        self.assertEqual(len([q for q, _ in store.cursor_obj.queries
                              if q.startswith('UPDATE swap_records')]), 1)
        self.assertEqual(store.connection.commits, 2)

    def test_rejects_tampered_payload_before_persistence(self):
        store = Store()
        tampered = {**BODY, 'take': {'asset': 'CR', 'amount': '171.00'}}
        with self.assertRaises(proposal.SellerProposalInconsistent):
            prepare(store, tampered)
        self.assertIsNone(store.cursor_obj.expires)

    def test_closed_or_expired_window_no_second_post(self):
        store = Store(Cursor(closes=NOW + timedelta(seconds=4)))
        with self.assertRaises(proposal.SellerProposalExpired):
            prepare(store)
        self.assertIsNone(store.cursor_obj.expires)
        store = Store()
        prepare(store)
        store.cursor_obj.now += timedelta(seconds=54)
        with self.assertRaises(proposal.SellerProposalExpired):
            prepare(store)
        self.assertEqual(len([q for q, _ in store.cursor_obj.queries
                              if q.startswith('UPDATE swap_records')]), 1)

    def test_rejects_missing_hold_and_identity(self):
        with self.assertRaises(proposal.SellerProposalInconsistent):
            prepare(Store(Cursor(held='RELEASED')))
        tampered = {**BODY, 'idempotencyKey': 'another-key'}
        with self.assertRaises(proposal.SellerProposalInconsistent):
            prepare(Store(), tampered)

    def test_http_202_does_not_complete_callback(self):
        history = []
        class TokenStore:
            def register_task_token(self, **kwargs):
                history.append('token-persisted')
                return {'status': 'WAITING', 'id': 1}
        class Wallet:
            def propose(self, **kwargs):
                history.append('wallet-post')
                self.kwargs = kwargs
                return wallet_client.WalletResult(
                    202, {'swapId': 'swap-123', 'status': 'accepted'}, True, IDPK
                )
        store = TokenStore()
        wallet = Wallet()
        event = {
            'taskToken': 'opaque', 'sagaExecutionId': IDPK,
            'correlationIdpk': IDPK, 'idempotencyKey': IDPK,
            'operation': 'PROPOSE', 'swap': BODY,
            'timeoutAt': '2099-01-01T00:00:00Z',
        }
        class Context:
            def get_remaining_time_in_millis(self):
                return 20000
        body = {'give': {'asset': 'KWH', 'amount': 40},
                'take': {'asset': 'CR', 'amount': 170.0},
                'memo': 'KLD-cycle', 'expiresAt': '2026-11-25T14:59:55Z'}
        with patch.object(handler, 'prepare_seller_proposal', side_effect=lambda *a, **k:
                          history.append('body-frozen') or body), \
             patch.object(handler, '_complete_known_callback', side_effect=AssertionError('202 no es terminal')):
            output = handler._callback_wallet_task('seller-proposal', event,
                                                    Context(), store, wallet)
        self.assertEqual(history, ['token-persisted', 'body-frozen', 'wallet-post'])
        self.assertTrue(output['acceptedForProcessing'])
        self.assertEqual(wallet.kwargs['idempotency_key'], IDPK)
        self.assertEqual(wallet.kwargs['swap'], body)

    def test_http_200_proposed_may_resume_callback(self):
        class Store:
            def register_task_token(self, **kwargs):
                return {'status': 'WAITING', 'id': 1}
        class Wallet:
            def propose(self, **kwargs):
                return wallet_client.WalletResult(200, {
                    'swapId': 'swap-123', 'expiresAt': '2026-11-25T15:00:00Z',
                    'status': 'PROPOSED',
                }, False, IDPK)
        event = {'taskToken': 'token', 'sagaExecutionId': IDPK,
                 'correlationIdpk': IDPK, 'idempotencyKey': IDPK,
                 'operation': 'PROPOSE', 'swap': BODY,
                 'timeoutAt': '2099-01-01T00:00:00Z'}
        with patch.object(handler, 'prepare_seller_proposal', return_value={}) as prep, \
             patch.object(handler, '_complete_known_callback') as success:
            handler._callback_wallet_task('seller-proposal', event, object(), Store(), Wallet())
        prep.assert_called_once()
        success.assert_called_once()
        self.assertEqual(success.call_args.kwargs['outcome']['outcome'], 'PROPOSED')


if __name__ == '__main__':
    unittest.main()
