"""E2-032: POST /swaps con reserva AD4, clave persistente y reintentos seguros."""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlmodel import Session, select

from app.config import CITY_ID
from app.database import engine, run_migrations
from app.main import app
from app.models import (
    Cycle, InboundMessage, MarketBid, MarketCycle, MarketOffer,
    OfferReservation, OutboundMessage, ProcessedIdpk, SwapRecord,
)
from app.services.seller_swap_dispatch import dispatch_swap_proposals
from app.services.wallet_client import (
    WalletClient, WalletError, WalletRateLimitError, WalletTimeoutError,
    WalletSwapWriteResponse,
)
from app.services.wallet_service import WalletService


def setup_module():
    assert (engine.url.database or '').startswith('energyshark_test_')
    run_migrations()


@pytest.fixture
def scenario():
    now = datetime.now(timezone.utc)
    cycle = f'e2032-{uuid4()}'
    offer = f'{CITY_ID}-{cycle}'
    buyer = 'COR' if CITY_ID != 'COR' else 'HGW'
    with Session(engine) as session:
        session.add(Cycle(
            cycle_id=cycle, created_at=now, status_idpk=str(uuid4()),
            market_opens_at=now-timedelta(minutes=1),
            market_last_bid_at=now+timedelta(minutes=3),
            market_last_offer_at=now+timedelta(minutes=4),
            market_closes_at=now+timedelta(minutes=5),
            valid_until=now+timedelta(minutes=6),
            status_payload={'market': {'minBid': 1}},
        ))
        session.add(MarketOffer(
            offer_id=offer, cycle_id=cycle, city_id=CITY_ID,
            is_local=True, initial_quantity=100, remaining_quantity=100,
            price_per_energy=Decimal('1.235'), status='ACTIVE',
            published_at=now, last_updated_at=now,
        ))
        session.commit()
    yield {'cycle': cycle, 'offer': offer, 'buyer': buyer, 'now': now}
    with Session(engine) as session:
        keys = session.exec(select(InboundMessage.idpk).where(
            InboundMessage.cycle_id == cycle, InboundMessage.idpk.is_not(None))).all()
        if keys:
            session.exec(delete(ProcessedIdpk).where(ProcessedIdpk.idpk.in_(keys)))
        session.exec(delete(OutboundMessage).where(OutboundMessage.cycle_id == cycle))
        session.exec(delete(InboundMessage).where(InboundMessage.cycle_id == cycle))
        session.exec(delete(SwapRecord).where(SwapRecord.cycle_id == cycle))
        session.exec(delete(OfferReservation).where(OfferReservation.offer_id == offer))
        session.exec(delete(MarketBid).where(MarketBid.cycle_id == cycle))
        session.exec(delete(MarketOffer).where(MarketOffer.offer_id == offer))
        session.exec(delete(MarketCycle).where(MarketCycle.cycle_id == cycle))
        session.exec(delete(Cycle).where(Cycle.cycle_id == cycle))
        session.commit()


def _incoming(s, quantity=3):
    msg = {'idpk': str(uuid4()), 'msgId': str(uuid4()), 'type': 'bid',
           'timestamp': datetime.now(timezone.utc).isoformat(),
           'cityId': s['buyer'], 'cycleId': s['cycle'],
           'data': {'offerId': s['offer'], 'quantity': quantity}}
    result = TestClient(app).post('/internal/messages', json=msg)
    assert result.status_code == 200, result.text
    assert result.json()['status'] == 'ok', result.text
    with Session(engine) as session:
        bid = session.exec(select(MarketBid).where(MarketBid.idpk == msg['idpk'])).one()
        reservation = session.exec(select(OfferReservation).where(
            OfferReservation.bid_idpk == msg['idpk'])).one()
        swap = session.exec(select(SwapRecord).where(SwapRecord.idpk == msg['idpk'])).one()
        assert bid.status == 'RESERVED' and reservation.status == 'HOLD'
        assert swap.status == 'SUBMITTED'
    return msg


def _record(idpk):
    with Session(engine) as session:
        return session.exec(select(SwapRecord).where(SwapRecord.idpk == idpk)).one()


def _reservation(idpk):
    with Session(engine) as session:
        return session.exec(select(OfferReservation).where(
            OfferReservation.bid_idpk == idpk)).one()


class FakeWallet:
    def __init__(self, results=None, *, delay=0):
        self.calls = []
        self.results = list(results or [])
        self.delay = delay

    async def propose_swap(self, **kwargs):
        self.calls.append(kwargs)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.results:
            result = self.results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return WalletSwapWriteResponse(
            status_code=202, data={}, idempotency_key=kwargs['idempotency_key'],
            accepted_for_processing=True,
        )


def test_post_swaps_real_http_client_has_exact_body_headers_and_rounding(scenario):
    bid = _incoming(scenario, quantity=3)
    # Decimal('3') * Decimal('1.235') = 3.705 => HALF_UP = 3.71.
    record = _record(bid['idpk'])
    assert record.take_amount == Decimal('3.71')
    wallet = WalletClient(base_url='https://wallet.test', api_key='test-secret')
    with patch('app.services.wallet_client.ObservedWalletAsyncClient') as observed:
        post = observed.return_value.__aenter__.return_value.post
        post.return_value = httpx.Response(202, json={'accepted': True})
        result = asyncio.run(dispatch_swap_proposals(limit=1, client=wallet))
        assert result['accepted_202'] == 1
        post.assert_awaited_once()
        args, kwargs = post.call_args
        assert args == ('https://wallet.test/swaps',)
        assert kwargs['headers']['X-Api-Key'] == 'test-secret'
        assert kwargs['headers']['Idempotency-Key'] == record.idempotency_key
        assert kwargs['json'] == {
            'give': {'asset': 'KWH', 'amount': 3.0},
            'take': {'asset': 'CR', 'amount': 3.71},
            'counterpart': scenario['buyer'],
            'memo': scenario['offer'],
        }
    after = _record(bid['idpk'])
    assert after.proposal_http_accepted_at is not None
    assert after.status == 'SUBMITTED'  # 202 != swap-proposed
    assert _reservation(bid['idpk']).status == 'HOLD'
    with Session(engine) as session:
        assert not session.exec(select(OutboundMessage).where(
            OutboundMessage.cycle_id == scenario['cycle'],
            OutboundMessage.message_type == 'bid-accepted')).all()


def test_http_202_does_not_repeat_and_does_not_accept_bid(scenario):
    bid = _incoming(scenario)
    wallet = FakeWallet()
    assert asyncio.run(dispatch_swap_proposals(client=wallet))['accepted_202'] == 1
    assert asyncio.run(dispatch_swap_proposals(client=wallet))['claimed'] == 0
    assert len(wallet.calls) == 1
    with Session(engine) as session:
        local = session.exec(select(MarketBid).where(MarketBid.idpk == bid['idpk'])).one()
        assert local.status == 'RESERVED'
        assert local.responded_at is None


def test_timeout_preserves_hold_and_retries_with_exact_same_key(scenario):
    bid = _incoming(scenario)
    wallet = FakeWallet([WalletTimeoutError('timeout')])
    first = asyncio.run(dispatch_swap_proposals(client=wallet))
    assert first['retry'] == 1
    pending = _record(bid['idpk'])
    assert pending.proposal_attempt_count == 1
    assert pending.proposal_retry_at is not None
    assert pending.proposal_http_accepted_at is None
    assert _reservation(bid['idpk']).status == 'HOLD'
    assert asyncio.run(dispatch_swap_proposals(client=wallet))['claimed'] == 0
    # Simular el avance del reloj sin dormir.
    later = pending.proposal_retry_at + timedelta(seconds=1)
    second = asyncio.run(dispatch_swap_proposals(client=wallet, now=later))
    assert second['accepted_202'] == 1
    assert len(wallet.calls) == 2
    assert wallet.calls[0] == wallet.calls[1]
    assert _record(bid['idpk']).proposal_attempt_count == 2
    assert _record(bid['idpk']).idempotency_key == pending.idempotency_key


def test_rate_limit_honors_retry_after(scenario):
    bid = _incoming(scenario)
    wallet = FakeWallet([WalletRateLimitError('429', status_code=429, retry_after=45)])
    assert asyncio.run(dispatch_swap_proposals(client=wallet))['retry'] == 1
    record = _record(bid['idpk'])
    assert record.proposal_last_error == 'RATE_LIMIT_429'
    assert record.proposal_retry_at >= datetime.now(timezone.utc) + timedelta(seconds=35)
    assert _reservation(bid['idpk']).status == 'HOLD'


def test_wallet_http_500_is_retryable(scenario):
    bid = _incoming(scenario)
    wallet = FakeWallet([WalletError('server unavailable', status_code=503)])
    assert asyncio.run(dispatch_swap_proposals(client=wallet))['retry'] == 1
    assert _record(bid['idpk']).proposal_last_error == 'WALLET_HTTP_503'
    assert _reservation(bid['idpk']).status == 'HOLD'


def test_http_400_pauses_without_releasing_hold(scenario):
    bid = _incoming(scenario)
    wallet = FakeWallet([WalletError('bad request', status_code=400)])
    assert asyncio.run(dispatch_swap_proposals(client=wallet))['blocked'] == 1
    record = _record(bid['idpk'])
    assert record.proposal_blocked_at is not None
    assert record.proposal_last_error == 'WALLET_HTTP_400'
    assert asyncio.run(dispatch_swap_proposals(client=wallet))['claimed'] == 0
    assert _reservation(bid['idpk']).status == 'HOLD'


def test_two_worker_loops_do_not_post_twice_simultaneously(scenario):
    bid = _incoming(scenario)
    wallet = FakeWallet(delay=0.08)

    async def both():
        return await asyncio.gather(
            dispatch_swap_proposals(client=wallet),
            dispatch_swap_proposals(client=wallet),
        )

    a, b = asyncio.run(both())
    assert a['claimed'] + b['claimed'] == 1
    assert len(wallet.calls) == 1
    assert _record(bid['idpk']).proposal_attempt_count == 1


def test_early_swap_proposed_event_does_not_get_downgraded_by_http_202(scenario):
    bid = _incoming(scenario)

    class WalletWithEvent(FakeWallet):
        async def propose_swap(self, **kwargs):
            with Session(engine) as session:
                record = WalletService().process_swap_event(
                    session, event_type='swap-proposed',
                    data={'idpk': bid['idpk'], 'swapId': 'sw-e2032'},
                )
                assert record.status == 'PROPOSED'
                session.commit()
            return await super().propose_swap(**kwargs)

    assert asyncio.run(dispatch_swap_proposals(client=WalletWithEvent()))['accepted_202'] == 1
    record = _record(bid['idpk'])
    assert record.status == 'PROPOSED'
    assert record.swap_id == 'sw-e2032'
    assert record.proposal_http_accepted_at is not None
    assert _reservation(bid['idpk']).status == 'HOLD'
