"""E3-048: métricas AD7 y recuperación de cancelación, solo base descartable."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import NAMESPACE_URL, uuid5
import asyncio

from sqlmodel import Session, select

from app.database import engine, run_migrations
from app.models import (Cycle, MarketBid, OfferReservation, SagaExecution,
                        SwapRecord)
from app.services.saga_recovery_health import recovery_health
from app.services.swap_timeouts import expire_stale_swaps
from app.services import swap_timeouts
from tests.test_e3041_seller_bid_accepted_db import seller_case
from tests.test_e3042_seller_terminal_db import _propose


def setup_module():
    assert (engine.url.database or "").startswith("energyshark_test_")
    run_migrations()


def test_overdue_execution_and_hold_are_counted_without_mutation(seller_case):
    now, cycle_id, offer_id, bid_idpk, _ = seller_case
    with Session(engine) as session:
        cycle = session.get(Cycle, cycle_id)
        cycle.market_closes_at = now - timedelta(seconds=80)
        session.add(cycle)
        session.commit()
    with Session(engine) as session:
        before_saga = session.exec(select(SagaExecution).where(
            SagaExecution.idpk == bid_idpk)).one()
        before_hold = session.exec(select(OfferReservation).where(
            OfferReservation.bid_idpk == bid_idpk)).one()
        assert before_hold.status == "HOLD"
        observed = recovery_health(session, now=now, sample_size=100)
        assert bid_idpk in observed["examples"]["overdueExecutionIdpks"]
        assert bid_idpk in observed["examples"]["heldBidIdpks"]
        assert observed["overdueExecutionsAfter60s"] >= 1
        assert observed["heldReservationsAfterClose"] >= 1
        assert before_saga.status in {"PENDING_START", "RUNNING", "WAITING"}
        assert session.exec(select(OfferReservation).where(
            OfferReservation.bid_idpk == bid_idpk)).one().status == "HOLD"


def test_timed_out_saga_with_unresolved_swap_is_visible(seller_case):
    now, cycle_id, offer_id, bid_idpk, _ = seller_case
    with Session(engine) as session:
        saga = session.exec(select(SagaExecution).where(
            SagaExecution.idpk == bid_idpk)).one()
        saga.status = "TIMED_OUT"
        session.add(saga)
        session.commit()
    with Session(engine) as session:
        observed = recovery_health(session, now=now, sample_size=100)
        assert bid_idpk in observed["examples"]["unresolvedSagaIdpks"]
        assert observed["unresolvedTimedOutOrFailedSwaps"] >= 1


def test_metrics_do_not_report_future_open_cycle(seller_case):
    now, cycle_id, offer_id, bid_idpk, _ = seller_case
    with Session(engine) as session:
        observed = recovery_health(session, now=now, sample_size=100)
        assert bid_idpk not in observed["examples"]["overdueExecutionIdpks"]
        assert bid_idpk not in observed["examples"]["heldBidIdpks"]


def test_post_cancel_get_is_authoritative_and_releases_only_after_confirm(seller_case, monkeypatch):
    now, cycle_id, offer_id, bid_idpk, _ = seller_case
    event = _propose(seller_case)
    swap_id = event["swapId"]
    with Session(engine) as session:
        row = session.exec(select(SwapRecord).where(SwapRecord.idpk == bid_idpk)).one()
        row.expires_at = now - timedelta(seconds=1)
        session.add(row)
        session.commit()

    class Wallet:
        def __init__(self):
            self.observed = 0
            self.keys = []
        async def get_swap(self, value):
            assert value == swap_id
            self.observed += 1
            return {"swapId": value, "status": "proposed" if self.observed == 1 else "cancelled"}
        async def cancel_swap(self, value, *, idempotency_key):
            assert value == swap_id
            self.keys.append(idempotency_key)
            return {"swapId": swap_id, "status": "accepted"}  # no terminal

    monkeypatch.setattr(swap_timeouts, "_stale_records", lambda session, now, limit:
                        [session.exec(select(SwapRecord).where(SwapRecord.idpk == bid_idpk)).one()])
    monkeypatch.setattr(swap_timeouts, "_stale_accepted_bids", lambda session, now: [])
    monkeypatch.setattr(swap_timeouts, "_stale_pending_bids", lambda session, now: [])
    client = Wallet()
    with Session(engine) as session:
        metrics = asyncio.run(expire_stale_swaps(
            session, now=now, client=client))
        session.commit()
    assert client.observed == 2
    assert client.keys == [str(uuid5(NAMESPACE_URL, f"energyshark:seller:cancel:{bid_idpk}"))]
    assert metrics["cancel_requested"] == 1
    with Session(engine) as session:
        row = session.exec(select(SwapRecord).where(SwapRecord.idpk == bid_idpk)).one()
        hold = session.exec(select(OfferReservation).where(
            OfferReservation.bid_idpk == bid_idpk)).one()
        assert row.status == "CANCELLED" and hold.status == "RELEASED"


def test_post_cancel_active_get_never_releases_hold(seller_case, monkeypatch):
    now, cycle_id, offer_id, bid_idpk, _ = seller_case
    event = _propose(seller_case)
    swap_id = event["swapId"]
    with Session(engine) as session:
        row = session.exec(select(SwapRecord).where(SwapRecord.idpk == bid_idpk)).one()
        row.expires_at = now - timedelta(seconds=1)
        session.add(row)
        session.commit()

    class Wallet:
        async def get_swap(self, value):
            return {"swapId": swap_id, "status": "proposed"}
        async def cancel_swap(self, value, *, idempotency_key):
            return {"swapId": swap_id, "status": "cancelled"}

    monkeypatch.setattr(swap_timeouts, "_stale_records", lambda session, now, limit:
                        [session.exec(select(SwapRecord).where(SwapRecord.idpk == bid_idpk)).one()])
    monkeypatch.setattr(swap_timeouts, "_stale_accepted_bids", lambda session, now: [])
    monkeypatch.setattr(swap_timeouts, "_stale_pending_bids", lambda session, now: [])
    with Session(engine) as session:
        metrics = asyncio.run(expire_stale_swaps(
            session, now=now, client=Wallet()))
        session.commit()
    assert metrics["indeterminate"] >= 1
    with Session(engine) as session:
        row = session.exec(select(SwapRecord).where(SwapRecord.idpk == bid_idpk)).one()
        hold = session.exec(select(OfferReservation).where(
            OfferReservation.bid_idpk == bid_idpk)).one()
        assert row.status == "PROPOSED" and hold.status == "HOLD"
