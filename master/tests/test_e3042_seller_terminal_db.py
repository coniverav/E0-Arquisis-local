"""E3-042: finalización y compensación seller con PostgreSQL de pruebas."""
import sys
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlmodel import Session, select

from app.database import engine, run_migrations
from app.models import MarketOffer, OfferReservation, SagaExecution, SagaTaskToken, SwapRecord
from app.services.wallet_service import WalletService
from app.services.seller_bid_accepted import enqueue_bid_accepted_after_swap_proposed
from tests.test_e3041_seller_bid_accepted_db import seller_case, _wallet_event

LAMBDA_DIR = Path(__file__).resolve().parents[2] / 'infra' / 'cdk' / 'lambda_src' / 'saga_task'
sys.path.insert(0, str(LAMBDA_DIR))
from seller_terminal import (  # noqa: E402
    SellerTerminalInconsistent, SellerTerminalPending,
    compensate_seller, finalize_seller,
)


def setup_module():
    assert (engine.url.database or '').startswith('energyshark_test_')
    run_migrations()


def _propose(case):
    now, cycle, offer, bid, store = case
    event, _ = _wallet_event(case)
    with Session(engine) as session:
        rec = WalletService(client=object()).process_swap_event(
            session, event_type='swap-proposed', data=event)
        assert rec is not None
        assert enqueue_bid_accepted_after_swap_proposed(
            session, record=rec, event_data=event, sender='central', now=now)
        session.commit()
    return event


def _wallet_terminal(case, event, event_type):
    with Session(engine) as session:
        record = WalletService(client=object()).process_swap_event(
            session, event_type=event_type,
            data={'swapId': event['swapId'], 'idpk': event['idpk']},
            rejection_reason='INSUFFICIENT_ASSET' if event_type == 'swap-rejected' else None,
        )
        assert record is not None
        session.commit()


def _state(case):
    _, cycle, offer, bid, _ = case
    with Session(engine) as session:
        saga = session.exec(select(SagaExecution).where(SagaExecution.idpk == bid)).one()
        record = session.exec(select(SwapRecord).where(SwapRecord.idpk == bid)).one()
        reservation = session.exec(select(OfferReservation).where(OfferReservation.bid_idpk == bid)).one()
        offer_row = session.exec(select(MarketOffer).where(MarketOffer.offer_id == offer)).one()
        return saga.status, record.status, reservation.status, offer_row.remaining_quantity


def test_settled_closes_saga_without_second_offer_decrement(seller_case):
    case = seller_case
    now, cycle, offer, bid, store = case
    event = _propose(case)
    _wallet_terminal(case, event, 'swap-settled')
    assert _state(case)[1:] == ('SETTLED', 'COMMITTED', 60)
    first = finalize_seller(store, saga_execution_id=bid, status='SETTLED')
    second = finalize_seller(store, saga_execution_id=bid, status='SETTLED')
    assert first['alreadyFinalized'] is False and second['alreadyFinalized'] is True
    assert _state(case) == ('SETTLED', 'SETTLED', 'COMMITTED', 60)
    assert first['swapId'] == event['swapId']


@pytest.mark.parametrize('kind,expected', [
    ('swap-rejected', 'REJECTED'), ('swap-expired', 'EXPIRED'),
    ('swap-cancelled', 'CANCELLED'),
])
def test_wallet_terminal_release_then_idempotent_compensation(seller_case, kind, expected):
    case = seller_case
    _, cycle, offer, bid, store = case
    event = _propose(case)
    _wallet_terminal(case, event, kind)
    assert _state(case)[1:] == (expected, 'RELEASED', 100)
    first = compensate_seller(store, bid_idpk=bid, offer_id=offer,
                              saga_execution_id=bid)
    second = compensate_seller(store, bid_idpk=bid, offer_id=offer,
                               saga_execution_id=bid)
    assert first['walletStatus'] == expected and not first['alreadyFinalized']
    assert second['alreadyFinalized']
    assert _state(case) == ('COMPENSATED', expected, 'RELEASED', 100)


def test_http_202_or_proposed_never_compensates_hold(seller_case):
    case = seller_case
    _, _, offer, bid, store = case
    for phase in ('SUBMITTED', 'PROPOSED'):
        if phase == 'PROPOSED':
            _propose(case)
        with pytest.raises(SellerTerminalPending):
            compensate_seller(store, bid_idpk=bid, offer_id=offer,
                              saga_execution_id=bid)
        assert _state(case)[2] == 'HOLD'
    assert _state(case)[0] not in {'COMPENSATED', 'SETTLED'}


def test_terminal_swap_without_released_hold_fails_closed(seller_case):
    case = seller_case
    _, _, offer, bid, store = case
    event = _propose(case)
    # Simula un estado terminal incoherente: la reserva sigue retenida.
    with Session(engine) as session:
        rec = session.exec(select(SwapRecord).where(SwapRecord.idpk == bid)).one()
        rec.status = 'CANCELLED'
        session.add(rec)
        session.commit()
    with pytest.raises(SellerTerminalInconsistent, match='RELEASE_NOT_APPLIED'):
        compensate_seller(store, bid_idpk=bid, offer_id=offer,
                          saga_execution_id=bid)
    assert _state(case)[2] == 'HOLD'
    assert _state(case)[0] not in {'COMPENSATED', 'SETTLED'}


def test_rollback_of_wallet_event_never_finalizes_saga(seller_case):
    case = seller_case
    _, _, offer, bid, store = case
    event = _propose(case)
    with Session(engine) as session:
        rec = WalletService(client=object()).process_swap_event(
            session, event_type='swap-rejected', data=event,
            rejection_reason='INSUFFICIENT_ASSET')
        assert rec is not None
        session.rollback()
    with pytest.raises(SellerTerminalPending):
        compensate_seller(store, bid_idpk=bid, offer_id=offer,
                          saga_execution_id=bid)
    assert _state(case)[2] == 'HOLD'


def test_finalization_supersedes_remaining_task_tokens(seller_case):
    case = seller_case
    now, cycle, offer, bid, store = case
    event = _propose(case)
    registered = store.register_task_token(
        saga_execution_id=bid, task_token='opaque-e3042-' + str(uuid4()),
        wait_type='TERMINAL', timeout_at=now + timedelta(minutes=3),
        correlation_idpk=None, swap_id=event['swapId'],
    )
    assert registered['status'] == 'WAITING'
    _wallet_terminal(case, event, 'swap-rejected')
    compensate_seller(store, bid_idpk=bid, offer_id=offer, saga_execution_id=bid)
    with Session(engine) as session:
        saga = session.exec(select(SagaExecution).where(SagaExecution.idpk == bid)).one()
        token = session.exec(select(SagaTaskToken).where(SagaTaskToken.id == registered['id'])).one()
        assert saga.status == 'COMPENSATED' and saga.terminal_at is not None
        assert saga.swap_id == event['swapId']
        assert token.status == 'SUPERSEDED'
        assert token.completed_at is not None


def test_conflicting_terminal_result_never_overwrites_compensation(seller_case):
    case = seller_case
    _, _, offer, bid, store = case
    event = _propose(case)
    _wallet_terminal(case, event, 'swap-expired')
    compensate_seller(store, bid_idpk=bid, offer_id=offer, saga_execution_id=bid)
    with pytest.raises(SellerTerminalInconsistent):
        finalize_seller(store, saga_execution_id=bid, status='SETTLED')
    assert _state(case)[0] == 'COMPENSATED'
