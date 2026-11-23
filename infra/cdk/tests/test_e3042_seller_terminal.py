"""E3-042: contrato Lambda seller terminal sin Docker, Wallet ni PostgreSQL."""
import importlib
import json
import re
from types import SimpleNamespace
from typing import Any
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / 'infra' / 'cdk' / 'lambda_src' / 'saga_task'
sys.path.insert(0, str(SOURCE))
terminal = importlib.import_module('seller_terminal')

BID = '820e9739-a3e6-4a6d-b4f4-7193a47d34ab'
CYCLE = 'cycle-e3042'
OFFER = 'KLD-cycle-e3042'
SWAP = 'swp-e3042'


class Cursor:
    def __init__(self, *, swap_status='SETTLED', reservation_status='COMMITTED',
                 saga_status='WAITING', swap_id=SWAP, offer=OFFER, bid_status='ACCEPTED'):
        self.swap_status = swap_status
        self.reservation_status = reservation_status
        self.saga_status = saga_status
        self.swap_id = swap_id
        self.offer = offer
        self.bid_status = bid_status
        self.queries = []
        self.last = ''
        self.rowcount = 0
        self.token_state = 'WAITING'

    def execute(self, sql, params):
        self.last = ' '.join(sql.split())
        self.queries.append((self.last, params))
        self.rowcount = 0
        if self.last.startswith('UPDATE saga_executions'):
            self.saga_status = params[0]
            self.rowcount = 1
        elif self.last.startswith('UPDATE saga_task_tokens'):
            self.token_state = 'SUPERSEDED'
            self.rowcount = 1

    def fetchone(self):
        q = self.last
        if q.startswith('SELECT id, role, cycle_id, swap_id, status FROM saga_executions'):
            return (27, 'SELLER', CYCLE, None, self.saga_status)
        if q.startswith('SELECT cycle_id, offer_id, counterpart_city_id, quantity,'):
            return (CYCLE, self.offer, 'COR', 40, self.bid_status, 'INCOMING', self.swap_id)
        if q.startswith('SELECT cycle_id, role, counterpart_city_id, memo, status,'):
            return (CYCLE, 'PROPOSER', 'COR', OFFER, self.swap_status,
                    self.swap_id, 'KWH', 40, 'CR')
        if q.startswith('SELECT offer_id, quantity, status FROM offer_reservations'):
            return (OFFER, 40, self.reservation_status)
        if q.startswith('SELECT se.role, se.cycle_id, sr.cycle_id, sr.role,'):
            return ('SELLER', CYCLE, CYCLE, 'PROPOSER', BID, self.swap_id)
        raise AssertionError('consulta no modelada: ' + q)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class Connection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class Store:
    def __init__(self, cursor):
        self.dsn = 'postgresql://fake/test'
        self.now = lambda: datetime(2026, 11, 23, tzinfo=timezone.utc)
        self.connection = Connection(cursor)
        self.connect = lambda _: self.connection


def _writes(cursor):
    return [query for query, _ in cursor.queries if query.startswith('UPDATE')]


class SellerTerminalTests(unittest.TestCase):
    def test_settled_closes_once_without_reapplying_ledger(self):
        cur = Cursor()
        store = Store(cur)
        a = terminal.finalize_seller(store, saga_execution_id=BID, status='SETTLED')
        b = terminal.finalize_seller(store, saga_execution_id=BID, status='SETTLED')
        self.assertEqual(a['status'], 'SETTLED')
        self.assertFalse(a['alreadyFinalized'])
        self.assertTrue(b['alreadyFinalized'])
        self.assertEqual(_writes(cur), [
            'UPDATE saga_executions SET status = %s, swap_id = %s, updated_at = %s, terminal_at = %s WHERE id = %s',
            "UPDATE saga_task_tokens SET status = 'SUPERSEDED', completed_at = %s WHERE saga_execution_id = %s AND status = 'WAITING'",
        ])
        self.assertEqual(cur.token_state, 'SUPERSEDED')

    def test_compensated_only_after_terminal_wallet_and_release(self):
        cur = Cursor(swap_status='REJECTED', reservation_status='RELEASED')
        first = terminal.compensate_seller(Store(cur), bid_idpk=BID,
                                            offer_id=OFFER, saga_execution_id=BID)
        second = terminal.compensate_seller(Store(cur), bid_idpk=BID,
                                             offer_id=OFFER, saga_execution_id=BID)
        self.assertEqual(first['walletStatus'], 'REJECTED')
        self.assertTrue(second['alreadyFinalized'])
        self.assertEqual(cur.reservation_status, 'RELEASED')

    def test_expired_and_cancelled_are_compensatable(self):
        for status in ('EXPIRED', 'CANCELLED'):
            with self.subTest(status=status):
                c = Cursor(swap_status=status, reservation_status='RELEASED')
                self.assertEqual(terminal.compensate_seller(
                    Store(c), bid_idpk=BID, offer_id=OFFER,
                    saga_execution_id=BID)['walletStatus'], status)

    def test_http_202_cannot_compensate(self):
        cur = Cursor(swap_status='SUBMITTED', reservation_status='HOLD')
        with self.assertRaises(terminal.SellerTerminalPending):
            terminal.compensate_seller(Store(cur), bid_idpk=BID,
                                       offer_id=OFFER, saga_execution_id=BID)
        self.assertEqual(_writes(cur), [])
        self.assertEqual(cur.reservation_status, 'HOLD')

    def test_proposed_cannot_compensate(self):
        cur = Cursor(swap_status='PROPOSED', reservation_status='HOLD')
        with self.assertRaises(terminal.SellerTerminalPending):
            terminal.compensate_seller(Store(cur), bid_idpk=BID,
                                       offer_id=OFFER, saga_execution_id=BID)
        self.assertEqual(_writes(cur), [])

    def test_terminal_wallet_without_applied_release_fails(self):
        cur = Cursor(swap_status='CANCELLED', reservation_status='HOLD')
        with self.assertRaisesRegex(terminal.SellerTerminalInconsistent,
                                    'RELEASE_NOT_APPLIED'):
            terminal.compensate_seller(Store(cur), bid_idpk=BID,
                                       offer_id=OFFER, saga_execution_id=BID)
        self.assertEqual(_writes(cur), [])

    def test_settlement_without_committed_reservation_fails(self):
        cur = Cursor(swap_status='SETTLED', reservation_status='HOLD')
        with self.assertRaisesRegex(terminal.SellerTerminalInconsistent,
                                    'SETTLEMENT_NOT_APPLIED'):
            terminal.finalize_seller(Store(cur), saga_execution_id=BID,
                                     status='SETTLED')
        self.assertEqual(_writes(cur), [])

    def test_reconciliation_never_guesses_unknown_swap(self):
        cur = Cursor(swap_id=None)
        with self.assertRaises(terminal.SellerTerminalPending):
            terminal.known_seller_swap_id(Store(cur), saga_execution_id=BID,
                                          idempotency_key=BID)
        self.assertEqual(_writes(cur), [])

    def test_wrong_identity_and_final_status_fail_before_db(self):
        cur = Cursor()
        with self.assertRaises(terminal.SellerTerminalInconsistent):
            terminal.compensate_seller(Store(cur), bid_idpk=BID,
                                       offer_id=OFFER, saga_execution_id='wrong')
        with self.assertRaises(terminal.SellerTerminalInconsistent):
            terminal.finalize_seller(Store(cur), saga_execution_id=BID, status='PROPOSED')
        self.assertEqual(cur.queries, [])

    def test_conflicting_terminal_states_never_reopen(self):
        cur = Cursor(swap_status='SETTLED', reservation_status='COMMITTED',
                     saga_status='COMPENSATED')
        with self.assertRaises(terminal.SellerTerminalInconsistent):
            terminal.finalize_seller(Store(cur), saga_execution_id=BID, status='SETTLED')
        self.assertEqual(_writes(cur), [])

    def test_wallet_reconciliation_normalizes_200_but_not_202(self):
        # Ejecuta la función real del handler sin inicializar Secrets/AWS.
        source = (SOURCE / 'handler.py').read_text()
        fragment = re.search(
            r'def _wallet_result\(.*?(?=\ndef _known_outcome\()',
            source, flags=re.S,
        )
        self.assertIsNotNone(fragment)
        ns = {'WalletResult': object, 'Any': Any}
        exec(fragment.group(), ns)
        wrapper = ns['_wallet_result']
        accepted = wrapper(SimpleNamespace(
            status_code=202, accepted_for_processing=True,
            idempotency_key=BID, data={'status': 'settled', 'swapId': SWAP},
        ))
        self.assertNotIn('status', accepted)
        self.assertNotIn('swapId', accepted)
        recovered = wrapper(SimpleNamespace(
            status_code=200, accepted_for_processing=False,
            idempotency_key=BID, data={
                'status': 'settled', 'swapId': SWAP, 'expiresAt': '2099-01-01T00:00:00Z',
            },
        ))
        self.assertEqual(recovered['status'], 'SETTLED')
        self.assertEqual(recovered['swapId'], SWAP)
        self.assertEqual(recovered['wallet']['status'], 'settled')

    def test_asl_seller_terminal_tasks_have_correct_routing(self):
        path = ROOT / 'infra' / 'cdk' / 'energyshark_infra' / 'asl' / 'seller.asl.json'
        states = json.loads(path.read_text())['States']
        self.assertEqual(states['CompensateReservation']['Next'], 'SellerCompensated')
        self.assertEqual(states['FinalizeSellerSettled']['Parameters']['Payload']['status'], 'SETTLED')
        for state in ('CompensateReservation', 'FinalizeSellerSettled'):
            self.assertIn('SellerTerminalPending', [error for retry in states[state].get('Retry', [])
                                                    for error in retry['ErrorEquals']])


if __name__ == '__main__':
    unittest.main()
