"""E3-045: contratos Lambda buyer sin red, AWS ni PostgreSQL."""
import importlib
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from jwt.algorithms import ECAlgorithm

SRC = Path(__file__).resolve().parents[1] / 'lambda_src' / 'saga_task'
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC))
from buyer_validation import (  # noqa: E402
    BuyerClaimsMismatch, BuyerExpiredToken, BuyerInvalidToken,
    BuyerJwksUnavailable, BuyerValidationInconsistent,
    _verify_jwt, check_buyer_deadline, jwks_settings, validate_buyer_token,
)
handler = importlib.import_module('handler')


class FakeCursor:
    def __init__(self, row):
        self.row = row
        self.queries = []
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def execute(self, query, args):
        self.queries.append((query, args))
    def fetchone(self):
        return self.row
    def cursor(self):
        return self


class FakeStore:
    dsn = 'postgresql://unit-test'
    def __init__(self, cursor, clock):
        self.cursor = cursor
        self.now = lambda: clock
    def connect(self, _dsn):
        return self.cursor


class BuyerValidationTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.expires = self.now + timedelta(minutes=2)
        self.key = ec.generate_private_key(ec.SECP256R1())
        jwk = json.loads(ECAlgorithm.to_jwk(self.key.public_key()))
        jwk.update({'kid': 'e3045-test', 'alg': 'ES256', 'use': 'sig'})
        self.jwks = {'keys': [jwk]}
        self.claims = {
            'iss': 'wallet.energyshark', 'sub': 'swap-1',
            'iat': int(self.now.timestamp()), 'exp': int(self.expires.timestamp()),
            'proposer': 'HGW', 'give': {'asset': 'KWH', 'amount': 6},
            'take': {'asset': 'CR', 'amount': 72}, 'memo': 'HGW-offer',
        }
        self.expected = {
            'proposer': 'HGW', 'give': {'asset': 'KWH', 'amount': '6'},
            'take': {'asset': 'CR', 'amount': '72.00'}, 'memo': 'HGW-offer',
        }
        self.cursor = FakeCursor((
            'BUYER', 'PENDING_START', 'cycle-1', 'swap-1',
            'accept-1', 'ACCEPTOR', 'swap-1', 'cycle-1', 'SUBMITTED',
            'HGW', 'CR', Decimal('72.0000'), 'KWH', Decimal('6.0000'),
            self.token(), self.expires, 'accept-1',
            'OUTGOING', 'ACCEPTED', 'cycle-1', 'HGW', 'swap-1',
            'HGW-offer', 6,
        ))
        self.store = FakeStore(self.cursor, self.now)

    def token(self, *, claims=None, key=None, algorithm='ES256'):
        return jwt.encode(claims or self.claims, key or self.key,
                          algorithm=algorithm, headers={'kid': 'e3045-test'})

    def verify(self, *, expected=None):
        return validate_buyer_token(
            self.store, saga_execution_id='accept-1', swap_record_id=7,
            swap_id='swap-1', expected=expected or self.expected,
            jwks_url='https://wallet.test/.well-known/jwks.json',
            issuer='wallet.energyshark', fetch_jwks=lambda url: self.jwks,
        )

    def test_valid_token_uses_db_and_returns_no_secret(self):
        result = self.verify()
        self.assertEqual(result, {'verified': True, 'swapId': 'swap-1',
                                  'sagaExecutionId': 'accept-1'})
        self.assertNotIn(self.cursor.row[14], json.dumps(result))
        self.assertIn('JOIN saga_executions', self.cursor.queries[0][0])
        self.assertEqual(self.cursor.queries[0][1], (7, 'accept-1'))

    def test_expected_claim_tampering_fails(self):
        changed = dict(self.expected, proposer='ATTACKER')
        with self.assertRaises(BuyerClaimsMismatch):
            self.verify(expected=changed)

    def test_unsigned_and_wrong_algorithm_fail(self):
        invalid = jwt.encode(self.claims, 'non-secret', algorithm='HS256',
                             headers={'kid': 'e3045-test'})
        self.cursor.row = self.cursor.row[:14] + (invalid,) + self.cursor.row[15:]
        with self.assertRaises(BuyerInvalidToken):
            self.verify()

    def test_token_signed_by_wrong_key_fails(self):
        wrong = self.token(key=ec.generate_private_key(ec.SECP256R1()))
        self.cursor.row = self.cursor.row[:14] + (wrong,) + self.cursor.row[15:]
        with self.assertRaises(BuyerInvalidToken):
            self.verify()

    def test_signed_claim_mismatch_fails(self):
        wrong = self.token(claims=dict(self.claims, sub='wrong-swap'))
        self.cursor.row = self.cursor.row[:14] + (wrong,) + self.cursor.row[15:]
        with self.assertRaises(BuyerClaimsMismatch):
            self.verify()

    def test_expiry_mismatch_fails_closed(self):
        self.cursor.row = self.cursor.row[:15] + (self.expires + timedelta(seconds=5),) + self.cursor.row[16:]
        with self.assertRaises(BuyerClaimsMismatch):
            self.verify()

    def test_expired_token_fails_even_if_jwks_unavailable(self):
        self.store.now = lambda: self.expires + timedelta(seconds=1)
        with self.assertRaises(BuyerExpiredToken):
            self.verify()

    def test_jwks_outage_is_not_invalid_signature(self):
        with self.assertRaises(BuyerJwksUnavailable):
            validate_buyer_token(
                self.store, saga_execution_id='accept-1', swap_record_id=7,
                swap_id='swap-1', expected=self.expected,
                jwks_url='https://wallet.test/.well-known/jwks.json',
                issuer='wallet.energyshark',
                fetch_jwks=lambda _: (_ for _ in ()).throw(OSError('offline')),
            )

    def test_wrong_saga_or_record_fails(self):
        with self.assertRaises(BuyerValidationInconsistent):
            validate_buyer_token(
                self.store, saga_execution_id='wrong', swap_record_id=7,
                swap_id='swap-1', expected=self.expected,
                jwks_url='https://wallet.test/.well-known/jwks.json',
                issuer='wallet.energyshark', fetch_jwks=lambda _: self.jwks,
            )
        # Una consulta con otra identidad recibe fila inconsistente.

    def test_jwks_root_from_wallet_secret_and_no_http(self):
        self.assertEqual(jwks_settings({'baseUrl': 'https://wallet.test/v1'})[0],
                         'https://wallet.test/.well-known/jwks.json')
        with self.assertRaises(BuyerValidationInconsistent):
            jwks_settings({'baseUrl': 'http://wallet.test/v1'})

    def test_deadline_uses_earliest_bound_and_margin(self):
        params = dict(saga_execution_id='accept-1', minimum_remaining_seconds=3)
        def iso(value):
            return value.isoformat()
        self.assertTrue(check_buyer_deadline(
            closes_at=iso(self.now+timedelta(seconds=10)),
            expires_at=iso(self.now+timedelta(seconds=4)),
            now=self.now, **params)['eligible'])
        self.assertFalse(check_buyer_deadline(
            closes_at=iso(self.now+timedelta(seconds=10)),
            expires_at=iso(self.now+timedelta(seconds=3)),
            now=self.now, **params)['eligible'])
        self.assertFalse(check_buyer_deadline(
            closes_at=iso(self.now-timedelta(seconds=1)),
            expires_at=iso(self.now+timedelta(minutes=1)),
            now=self.now, **params)['eligible'])

    def test_handler_deadline_never_builds_store(self):
        now = datetime.now(timezone.utc)
        event = dict(sagaExecutionId='accept-1',
                     closesAt=(now+timedelta(minutes=1)).isoformat(),
                     expiresAt=(now+timedelta(minutes=1)).isoformat(),
                     minimumRemainingSeconds=3)
        with patch.dict(os.environ, {'SAGA_TASK': 'check-buyer-deadline'}), \
             patch.object(handler, '_build_store', side_effect=AssertionError('DB not allowed')):
            result = handler.lambda_handler(event, object())
        self.assertTrue(result['eligible'])

    def test_asl_routes_python_error_names_and_payload(self):
        asl = json.loads((ROOT/'energyshark_infra'/'asl'/'buyer.asl.json').read_text())
        state = asl['States']['ValidateSwapToken']
        self.assertIn('BuyerInvalidToken', state['Catch'][0]['ErrorEquals'])
        self.assertIn('BuyerExpiredToken', state['Catch'][0]['ErrorEquals'])
        self.assertIn('BuyerClaimsMismatch', state['Catch'][0]['ErrorEquals'])
        self.assertIn('BuyerJwksUnavailable', state['Retry'][0]['ErrorEquals'])
        self.assertNotIn('swapToken', json.dumps(state))
        self.assertEqual(asl['States']['CheckBuyerDeadline']['Parameters']['Payload']
                         ['expiresAt.$'], '$.swap.expiresAt')

if __name__ == '__main__':
    unittest.main()
