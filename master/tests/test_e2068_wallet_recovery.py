"""E2-068 / AD5: 202 perdido, GET autorizado, idpk estable y eventos reordenados.

Pruebas unitarias sin acceso a red; las de integración existentes cubren PG,
claims de seller, ledger y reservas. Esta tarjeta no requiere migración.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from app.services.wallet_client import WalletError, WalletTimeoutError
from app.services.wallet_recovery import (
    WalletObservation, inspect_known_swap, apply_wallet_observation,
)
from app.services.seller_swap_dispatch import ProposalAttempt, _finish
from app.services.swap_accept import _accept_record
from app.services import swap_timeouts

NOW = datetime.now(timezone.utc)


class QueryWallet:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.ids = []

    async def get_swap(self, swap_id):
        self.ids.append(swap_id)
        if self.error:
            raise self.error
        return self.response


def _record(role='ACCEPTOR', **updates):
    fields = dict(
        idpk=str(uuid4()), swap_id='swp-e2068', role=role,
        status='ACCEPT_SUBMITTED' if role == 'ACCEPTOR' else 'SUBMITTED',
        accept_attempts=1, accept_error='WALLET_TIMEOUT',
        accept_idempotency_key=str(uuid4()),
        expires_at=NOW + timedelta(seconds=60),
        proposal_claim_token='lease-1', proposal_retry_at=None,
        proposal_http_accepted_at=None, proposal_lease_until=NOW,
        proposal_last_error=None, proposal_blocked_at=None,
        proposal_expires_at=NOW + timedelta(seconds=50),
        proposal_attempt_count=1, rejection_reason=None,
        accept_attempted_at=None,
    )
    fields.update(updates)
    return SimpleNamespace(**fields)


def _session(record=None):
    session = MagicMock()
    session.exec.return_value.first.return_value = record
    return session


def test_without_swap_id_no_get_is_attempted():
    client = QueryWallet({'status': 'settled'})
    assert asyncio.run(inspect_known_swap(client, None)) is None
    assert client.ids == []


def test_known_swap_get_accepted():
    client = QueryWallet({'swapId': 'swp-e2068', 'status': 'accepted'})
    obs = asyncio.run(inspect_known_swap(client, 'swp-e2068'))
    assert obs.status == 'accepted'
    assert client.ids == ['swp-e2068']


def test_get_swap_id_mismatch_cannot_converge():
    client = QueryWallet({'swapId': 'otra', 'status': 'settled'})
    assert asyncio.run(inspect_known_swap(client, 'swp-e2068')) is None


def test_get_unknown_status_is_not_terminal():
    client = QueryWallet({'status': 'unknown'})
    assert asyncio.run(inspect_known_swap(client, 'swp-e2068')) is None


def test_get_404_is_indeterminate_not_proof_of_no_post():
    client = QueryWallet(error=WalletError('404',status_code=404))
    assert asyncio.run(inspect_known_swap(client, 'swp-e2068')) is None


def test_get_timeout_is_indeterminate():
    client = QueryWallet(error=WalletTimeoutError('timeout'))
    assert asyncio.run(inspect_known_swap(client, 'swp-e2068')) is None


def test_acceptor_proposed_is_not_proof_of_acceptance():
    record = _record()
    session = _session()
    applied = apply_wallet_observation(session, record=record,
        observation=WalletObservation('swp-e2068', 'proposed'))
    assert applied is False
    assert record.accept_error == 'WALLET_TIMEOUT'


def test_acceptor_get_accepted_skips_duplicate_post():
    record = _record()
    session = _session()
    applied = apply_wallet_observation(session, record=record,
        observation=WalletObservation('swp-e2068', 'accepted'))
    assert applied is True
    assert record.status == 'ACCEPT_SUBMITTED'
    assert record.accept_error is None
    assert record.accept_idempotency_key


def test_proposer_get_proposed_does_not_claim_http_202():
    record = _record(role='PROPOSER')
    session = _session()
    applied = apply_wallet_observation(session, record=record,
        observation=WalletObservation('swp-e2068','proposed'))
    assert applied is True
    assert record.status == 'PROPOSED'
    assert record.proposal_http_accepted_at is None


def test_immutable_terminal_state_is_not_reverted():
    record = _record(status='SETTLED')
    session = _session()
    assert apply_wallet_observation(session, record=record,
        observation=WalletObservation('swp-e2068','accepted')) is True
    assert record.status == 'SETTLED'
    session.add.assert_not_called()


def test_swap_id_mismatch_has_no_side_effects():
    record = _record()
    session = _session()
    assert not apply_wallet_observation(session, record=record,
        observation=WalletObservation('otra','settled'))
    session.add.assert_not_called()


def test_get_settled_reuses_wallet_event_accounting(monkeypatch):
    from app.services import wallet_recovery
    calls = []
    class FakeService:
        def process_swap_event(self, session, *, event_type, data):
            calls.append((event_type, data))
    monkeypatch.setattr(wallet_recovery, 'WalletService', FakeService)
    record = _record(role='PROPOSER')
    applied = apply_wallet_observation(_session(), record=record,
        observation=WalletObservation('swp-e2068','settled'))
    assert applied is True
    assert calls == [('swap-settled',{'swapId':'swp-e2068','idpk':record.idpk})]


def test_seller_late_timeout_does_not_downgrade_settled():
    record = _record(role='PROPOSER',status='SETTLED')
    session = _session(record)
    attempt = ProposalAttempt(
        idpk=record.idpk, claim_token='lease-1', idempotency_key=str(uuid4()),
        give_amount=Decimal('3'), take_amount=Decimal('15'),
        counterpart_city_id='MET', memo='offer',
        expires_at=NOW + timedelta(seconds=40), attempt_count=2,
    )
    _finish(session, attempt, now=NOW, outcome='RETRY',error_code='TIMEOUT_OR_NETWORK')
    assert record.status == 'SETTLED'
    assert record.proposal_retry_at is None
    assert record.proposal_claim_token is None


class AcceptService:
    def __init__(self, wallet_status=None, *, finalize_during_http=False):
        self.client = QueryWallet(
            {'swapId':'swp-e2068','status':wallet_status} if wallet_status else None)
        self.calls = []
        self.finalize = finalize_during_http

    async def execute_accept_swap(self, session, record):
        self.calls.append(record.accept_idempotency_key)
        if self.finalize:
            record.status = 'SETTLED'  # simula evento AMQP antes de respuesta HTTP
        return SimpleNamespace(status_code=202,accepted_for_processing=True)


def test_retry_uses_get_accepted_before_post():
    record = _record()
    service = AcceptService('accepted')
    result = asyncio.run(_accept_record(_session(),record,service,NOW))
    assert result == 'recovered'
    assert service.calls == []
    assert record.accept_attempts == 1


def test_retry_if_get_still_proposed_reuses_same_idpk():
    record = _record()
    initial_key = record.accept_idempotency_key
    service = AcceptService('proposed')
    result = asyncio.run(_accept_record(_session(),record,service,NOW))
    assert result == 'accepted'
    assert service.calls == [initial_key]
    assert record.accept_idempotency_key == initial_key
    assert record.accept_attempts == 2


def test_event_during_http_wins_over_202():
    record = _record(accept_attempts=0,status='SUBMITTED')
    service = AcceptService(finalize_during_http=True)
    result = asyncio.run(_accept_record(_session(),record,service,NOW))
    assert result == 'recovered'
    assert record.status == 'SETTLED'
    assert len(service.calls) == 1


def test_ambiguous_proposal_never_expires_locally(monkeypatch):
    record = _record(role='PROPOSER', swap_id=None,
                     proposal_expires_at=NOW-timedelta(seconds=20),
                     proposal_attempt_count=1,status='SUBMITTED')
    monkeypatch.setattr(swap_timeouts, '_stale_records',
                        lambda *_a, **_k: [record])
    monkeypatch.setattr(swap_timeouts, '_stale_accepted_bids',
                        lambda *_a, **_k: [])
    monkeypatch.setattr(swap_timeouts, '_stale_pending_bids',
                        lambda *_a, **_k: [])
    result = asyncio.run(swap_timeouts.expire_stale_swaps(
        _session(),now=NOW,client=QueryWallet()))
    assert result['indeterminate'] == 1
    assert result['released_holds'] == 0
    assert record.status == 'SUBMITTED'
