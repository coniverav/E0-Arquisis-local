"""E3-040: contrato Wallet y expiresAt durable en PostgreSQL real.

Usa solamente la base de pruebas energyshark_test_* (conftest).
No invoca AWS ni la Wallet.
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlmodel import Session, select

from app.config import CITY_ID
from app.database import engine
from app.models import Cycle, MarketOffer, MarketBid, OfferReservation, SwapRecord
from app.services.seller_bid_acceptance import reserve_incoming_bid

SOURCE = Path(__file__).resolve().parents[2] / 'infra' / 'cdk' / 'lambda_src' / 'saga_task'
sys.path.insert(0, str(SOURCE))
from seller_proposal import (  # noqa: E402
    prepare_seller_proposal, SellerProposalExpired, SellerProposalInconsistent,
)
from postgres_store import PostgresStore  # noqa: E402

ARN = 'arn:aws:states:us-east-1:123456789012:stateMachine:test-e3040-seller'


@pytest.fixture
def seller_case(monkeypatch):
    monkeypatch.setenv('E3_SELLER_SAGA_ENABLED', 'true')
    monkeypatch.setenv('E3_SELLER_STATE_MACHINE_ARN', ARN)
    now = datetime.now(timezone.utc)
    cycle_id = 'e3040-' + str(uuid4())
    offer_id = f'{CITY_ID}-{cycle_id}'
    bid_idpk = str(uuid4())
    with Session(engine) as session:
        session.add(Cycle(
            cycle_id=cycle_id, status_idpk=str(uuid4()), created_at=now,
            market_opens_at=now - timedelta(minutes=1),
            market_last_offer_at=now + timedelta(minutes=4),
            market_last_bid_at=now + timedelta(minutes=3),
            market_closes_at=now + timedelta(minutes=5),
            valid_until=now + timedelta(minutes=6),
            status_payload={'market': {'minBid': 5}},
        ))
        session.add(MarketOffer(
            offer_id=offer_id, cycle_id=cycle_id, city_id=CITY_ID,
            is_local=True, initial_quantity=100, remaining_quantity=100,
            price_per_energy=Decimal('4.25'), status='ACTIVE',
            published_at=now, last_updated_at=now,
        ))
        session.add(MarketBid(
            idpk=bid_idpk, msg_id=str(uuid4()), cycle_id=cycle_id,
            direction='INCOMING', offer_id=offer_id,
            counterpart_city_id='COR', quantity=40,
            status='PENDING', created_at=now,
        ))
        session.commit()
    with Session(engine) as session:
        assert reserve_incoming_bid(session, bid_idpk=bid_idpk, now=now)['status'] == 'RESERVED'
        session.commit()
    store = PostgresStore(
        engine.url.render_as_string(hide_password=False).replace(
            'postgresql+psycopg://', 'postgresql://', 1
        ),
        city_id=CITY_ID, now=lambda: now,
    )
    return now, cycle_id, offer_id, bid_idpk, store


def _request(seller_case):
    _, _, offer_id, bid_idpk, store = seller_case
    return store, dict(
        saga_execution_id=bid_idpk, idempotency_key=bid_idpk,
        swap={
            'idpk': bid_idpk, 'idempotencyKey': bid_idpk,
            'cancelIdempotencyKey': str(uuid4()),
            'give': {'asset': 'KWH', 'amount': '40'},
            'take': {'asset': 'CR', 'amount': '170.00'},
            'memo': offer_id,
        },
    )


def test_freeze_post_body_transactionally_and_replay_exactly(seller_case):
    now, _, offer_id, bid_idpk, store = seller_case
    store, params = _request(seller_case)
    first = prepare_seller_proposal(store, **params)
    assert first == {
        'give': {'asset': 'KWH', 'amount': 40},
        'take': {'asset': 'CR', 'amount': 170.0},
        'memo': offer_id,
        'expiresAt': (now + timedelta(seconds=55)).isoformat().replace('+00:00', 'Z'),
    }
    with Session(engine) as session:
        before = session.exec(select(SwapRecord).where(SwapRecord.idpk == bid_idpk)).one()
        assert before.proposal_expires_at == now + timedelta(seconds=55)
        assert before.status == 'SUBMITTED'
        assert session.exec(select(OfferReservation).where(
            OfferReservation.bid_idpk == bid_idpk)).one().status == 'HOLD'
    assert prepare_seller_proposal(store, **params) == first
    with Session(engine) as session:
        record = session.exec(select(SwapRecord).where(SwapRecord.idpk == bid_idpk)).one()
        assert record.proposal_expires_at == now + timedelta(seconds=55)


def test_invalid_input_never_freezes_proposal(seller_case):
    _, _, _, bid_idpk, store = seller_case
    store, params = _request(seller_case)
    params['swap']['take']['amount'] = '100.00'
    with pytest.raises(SellerProposalInconsistent):
        prepare_seller_proposal(store, **params)
    with Session(engine) as session:
        record = session.exec(select(SwapRecord).where(SwapRecord.idpk == bid_idpk)).one()
        assert record.proposal_expires_at is None


def test_no_new_wallet_proposal_when_close_is_imminent(seller_case):
    now, _, _, bid_idpk, store = seller_case
    store, params = _request(seller_case)
    store.now = lambda: now + timedelta(minutes=4, seconds=57)
    with pytest.raises(SellerProposalExpired):
        prepare_seller_proposal(store, **params)
    with Session(engine) as session:
        record = session.exec(select(SwapRecord).where(SwapRecord.idpk == bid_idpk)).one()
        assert record.proposal_expires_at is None
