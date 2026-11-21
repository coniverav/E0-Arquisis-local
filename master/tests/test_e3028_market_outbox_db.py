"""E3-028 AD8: integración PostgreSQL real. SOLO energyshark_test_*.

Sin RabbitMQ, Wallet externa, AWS ni credenciales: verifica invariantes de
persistencia, rollback, redelivery y ventanas de los envelopes de mercado.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError, DBAPIError
from sqlmodel import Session, select

from app.config import CITY_ID
from app.database import engine
from app.models import Cycle, MarketOffer, NegotiationReport, OutboundMessage, StrategyConfig
from app.services.market_outbox import enqueue_market_envelope
from app.services.negotiation_report_dispatch import (
    enqueue_due_negotiation_reports, process_report_error,
)
from app.services.seller_offer import enqueue_seller_offer
from app.services.seller_offer_events import (
    change_local_offer_price, withdraw_local_offer,
)
from app.services.outbox_claims import claim_batch, finish_claim


def _rows(session, cycle_id, kind):
    return list(session.exec(select(OutboundMessage).where(
        OutboundMessage.cycle_id == cycle_id,
        OutboundMessage.message_type == kind,
    ).order_by(OutboundMessage.id)).all())


def _assert_envelope(row, kind, cycle_id):
    assert row.delivery_kind == "BROKER"
    assert row.message_type == kind
    assert row.cycle_id == cycle_id
    assert row.status == "PENDING"
    assert row.dispatch_required is True
    assert row.payload["msgId"] == row.msg_id
    assert row.payload["idpk"] == row.idpk
    assert row.payload["type"] == kind
    assert row.payload["cycleId"] == cycle_id
    assert row.payload["cityId"]
    assert row.payload["timestamp"]
    assert isinstance(row.payload["data"], dict)
    assert row.attempt_count == 0
    if kind == "negotiation-report":
        assert row.exchange_name == "energy.x"
        assert row.routing_key == "central"
    else:
        assert row.exchange_name == "market"
        assert row.routing_key == f"market.offer.{CITY_ID}"


@pytest.fixture
def offer_scenario():
    now = datetime.now(timezone.utc)
    cid = f"e3028-offer-{uuid4()}"
    oid = f"{CITY_ID}-{cid}"
    with Session(engine) as session:
        session.add(Cycle(
            cycle_id=cid, created_at=now,
            status_idpk=str(uuid4()),
            market_opens_at=now - timedelta(minutes=2),
            market_last_offer_at=now + timedelta(minutes=5),
            market_last_bid_at=now + timedelta(minutes=6),
            market_closes_at=now + timedelta(minutes=7),
            valid_until=now + timedelta(minutes=8),
        ))
        session.add(MarketOffer(
            offer_id=oid, cycle_id=cid, city_id=CITY_ID,
            is_local=True, initial_quantity=100, remaining_quantity=100,
            price_per_energy=Decimal("4.25"), status="ACTIVE",
            published_at=now, last_updated_at=now,
        ))
        session.commit()
    return SimpleNamespace(cid=cid, oid=oid, now=now)


@pytest.fixture
def report_scenario():
    now = datetime.now(timezone.utc)
    cid = f"e3028-report-{uuid4()}"
    with Session(engine) as session:
        session.add(Cycle(
            cycle_id=cid, created_at=now, status_idpk=str(uuid4()),
            scheduler_state="REPORT_WINDOW",
            report_window_opens_at=now - timedelta(seconds=30),
            report_window_opened_at=now,
            valid_until=now + timedelta(minutes=4),
            opening_budget_balance=Decimal("1000.00"),
            opening_energy_balance=Decimal("50.00"),
            budget_balance=Decimal("1000.00"),
            energy_balance=Decimal("50.00"),
            last_sequence=0, status_payload={},
        ))
        session.commit()
    return SimpleNamespace(cid=cid, now=now)


def test_offer_created_with_outbox_same_commit_and_stable_replay():
    now = datetime.now(timezone.utc)
    cid = f"e3028-create-{uuid4()}"
    oid = f"{CITY_ID}-{cid}"
    with Session(engine) as session:
        session.add(StrategyConfig(version=1, status="ACTIVE", created_at=now - timedelta(minutes=3)))
        session.flush()
        session.add(Cycle(
            cycle_id=cid, created_at=now,
            status_idpk=str(uuid4()), strategy_version=1,
            market_opens_at=now - timedelta(minutes=1),
            market_last_offer_at=now + timedelta(minutes=5),
            market_last_bid_at=now + timedelta(minutes=5),
            market_closes_at=now + timedelta(minutes=6),
            valid_until=now + timedelta(minutes=7),
            status_payload={"energy": {"sellable": 100, "generationCost": 3.0},
                            "market": {"buybackPrice": 4.0, "minBid": 5}},
        ))
        session.commit()

    class FakeWallet:
        async def get_balance(self):
            return SimpleNamespace(kwh_available=Decimal("1000"))

    assert asyncio.run(enqueue_seller_offer(cid, wallet_client=FakeWallet())) is True
    assert asyncio.run(enqueue_seller_offer(cid, wallet_client=FakeWallet())) is False
    with Session(engine) as session:
        offer = session.exec(select(MarketOffer).where(MarketOffer.offer_id == oid)).one()
        messages = _rows(session, cid, "offer")
        assert len(messages) == 1
        outbound = messages[0]
        _assert_envelope(outbound, "offer", cid)
        assert outbound.payload["data"]["offerId"] == offer.offer_id
        assert outbound.payload["data"]["quantity"] == offer.initial_quantity
        assert Decimal(str(outbound.payload["data"]["pricePerEnergy"])) == offer.price_per_energy
        original = (outbound.msg_id, outbound.idpk, deepcopy(outbound.payload))
        assert outbound.available_at == session.get(Cycle, cid).market_opens_at
        assert outbound.expires_at == session.get(Cycle, cid).market_last_offer_at
    with Session(engine) as session:
        replay = _rows(session, cid, "offer")
        assert len(replay) == 1
        assert (replay[0].msg_id, replay[0].idpk, replay[0].payload) == original


def test_offer_creation_failure_rolls_back_offer_and_envelope(monkeypatch):
    now = datetime.now(timezone.utc)
    cid = f"e3028-create-fail-{uuid4()}"
    oid = f"{CITY_ID}-{cid}"
    with Session(engine) as session:
        session.add(StrategyConfig(version=1, status="ACTIVE", created_at=now - timedelta(minutes=3)))
        session.flush()
        session.add(Cycle(
            cycle_id=cid, created_at=now, status_idpk=str(uuid4()), strategy_version=1,
            market_opens_at=now - timedelta(minutes=1),
            market_last_offer_at=now + timedelta(minutes=5),
            market_closes_at=now + timedelta(minutes=6),
            valid_until=now + timedelta(minutes=7),
            status_payload={"energy": {"sellable": 100, "generationCost": 3.0},
                            "market": {"buybackPrice": 4.0, "minBid": 5}},
        ))
        session.commit()

    class FakeWallet:
        async def get_balance(self):
            return SimpleNamespace(kwh_available=Decimal("1000"))

    def fail(*args, **kwargs):
        raise RuntimeError("simulated outage before commit")

    monkeypatch.setattr("app.services.seller_offer.enqueue_market_envelope", fail)
    with pytest.raises(RuntimeError, match="simulated outage"):
        asyncio.run(enqueue_seller_offer(cid, wallet_client=FakeWallet()))
    with Session(engine) as session:
        assert session.exec(select(MarketOffer).where(MarketOffer.offer_id == oid)).first() is None
        assert _rows(session, cid, "offer") == []


def test_update_persists_business_and_outbox(offer_scenario):
    ctx = offer_scenario
    with Session(engine) as session:
        assert change_local_offer_price(session, offer_id=ctx.oid, price=Decimal("5.50"))
        assert change_local_offer_price(session, offer_id=ctx.oid, price=Decimal("5.50")) is False
        session.commit()
    with Session(engine) as session:
        offer = session.exec(select(MarketOffer).where(MarketOffer.offer_id == ctx.oid)).one()
        messages = _rows(session, ctx.cid, "offer-updated")
        assert len(messages) == 1
        _assert_envelope(messages[0], "offer-updated", ctx.cid)
        assert messages[0].payload["data"] == {
            "offerId": ctx.oid, "remaining": 100, "pricePerEnergy": 5.5,
        }
        assert offer.price_per_energy == Decimal("5.50")


def test_update_rollback_removes_envelope_and_price_change(offer_scenario):
    ctx = offer_scenario
    with Session(engine) as session:
        assert change_local_offer_price(session, offer_id=ctx.oid, price=Decimal("7.00"))
        assert len(_rows(session, ctx.cid, "offer-updated")) == 1
        session.rollback()
    with Session(engine) as session:
        assert session.exec(select(MarketOffer).where(MarketOffer.offer_id == ctx.oid)).one().price_per_energy == Decimal("4.25")
        assert _rows(session, ctx.cid, "offer-updated") == []


def test_update_failure_cannot_leave_orphan_envelope(offer_scenario, monkeypatch):
    ctx = offer_scenario
    def fail(*args, **kwargs):
        raise RuntimeError("simulated outbox insert failure")
    monkeypatch.setattr("app.services.seller_offer_events.enqueue_market_envelope", fail)
    with Session(engine) as session:
        with pytest.raises(RuntimeError, match="insert failure"):
            change_local_offer_price(session, offer_id=ctx.oid, price=Decimal("9.00"))
        session.rollback()
    with Session(engine) as session:
        assert session.exec(select(MarketOffer).where(MarketOffer.offer_id == ctx.oid)).one().price_per_energy == Decimal("4.25")
        assert _rows(session, ctx.cid, "offer-updated") == []


def test_withdraw_commit_is_idempotent_and_has_full_envelope(offer_scenario):
    ctx = offer_scenario
    with Session(engine) as session:
        assert withdraw_local_offer(session, offer_id=ctx.oid)
        assert withdraw_local_offer(session, offer_id=ctx.oid) is False
        session.commit()
    with Session(engine) as session:
        offer = session.exec(select(MarketOffer).where(MarketOffer.offer_id == ctx.oid)).one()
        messages = _rows(session, ctx.cid, "offer-withdrawn")
        assert offer.status == "WITHDRAWN"
        assert len(messages) == 1
        _assert_envelope(messages[0], "offer-withdrawn", ctx.cid)
        assert messages[0].payload["data"] == {"offerId": ctx.oid}


def test_withdraw_rollback_does_not_leave_outbox(offer_scenario):
    ctx = offer_scenario
    with Session(engine) as session:
        assert withdraw_local_offer(session, offer_id=ctx.oid)
        session.rollback()
    with Session(engine) as session:
        assert session.exec(select(MarketOffer).where(MarketOffer.offer_id == ctx.oid)).one().status == "ACTIVE"
        assert _rows(session, ctx.cid, "offer-withdrawn") == []


def test_report_and_envelope_share_commit_and_identity(report_scenario):
    ctx = report_scenario
    with Session(engine) as session:
        assert enqueue_due_negotiation_reports(session, city_id=CITY_ID,
                                               routing_key="central", now=ctx.now) == 1
        assert enqueue_due_negotiation_reports(session, city_id=CITY_ID,
                                               routing_key="central", now=ctx.now) == 0
        session.commit()
    with Session(engine) as session:
        report = session.exec(select(NegotiationReport).where(NegotiationReport.cycle_id == ctx.cid)).one()
        rows = _rows(session, ctx.cid, "negotiation-report")
        assert len(rows) == 1
        _assert_envelope(rows[0], "negotiation-report", ctx.cid)
        assert (rows[0].msg_id, rows[0].idpk) == (report.msg_id, report.idpk)
        assert rows[0].payload == report.payload
        assert rows[0].payload["data"] == {"budgetBalance": 1000, "energyBalance": 50}
        assert rows[0].expires_at == session.get(Cycle, ctx.cid).valid_until


def test_report_rollback_removes_report_and_outbox(report_scenario):
    ctx = report_scenario
    with Session(engine) as session:
        assert enqueue_due_negotiation_reports(session, city_id=CITY_ID,
                                               routing_key="central", now=ctx.now) == 1
        session.rollback()
    with Session(engine) as session:
        assert session.exec(select(NegotiationReport).where(NegotiationReport.cycle_id == ctx.cid)).all() == []
        assert _rows(session, ctx.cid, "negotiation-report") == []


def test_report_outbox_failure_cannot_commit_only_business(report_scenario, monkeypatch):
    ctx = report_scenario
    def fail(*args, **kwargs):
        raise RuntimeError("simulated report enqueue outage")
    monkeypatch.setattr("app.services.negotiation_report_dispatch.enqueue_market_envelope", fail)
    with Session(engine) as session:
        with pytest.raises(RuntimeError, match="report enqueue outage"):
            enqueue_due_negotiation_reports(session, city_id=CITY_ID,
                                             routing_key="central", now=ctx.now)
        session.rollback()
    with Session(engine) as session:
        assert session.exec(select(NegotiationReport).where(NegotiationReport.cycle_id == ctx.cid)).all() == []
        assert _rows(session, ctx.cid, "negotiation-report") == []


def test_report_too_early_creates_new_msgid_same_idpk_without_mutation(report_scenario):
    ctx = report_scenario
    with Session(engine) as session:
        assert enqueue_due_negotiation_reports(session, city_id=CITY_ID,
                                               routing_key="central", now=ctx.now) == 1
        session.commit()
    with Session(engine) as session:
        old = _rows(session, ctx.cid, "negotiation-report")[0]
        old_id = old.msg_id
        old_idpk = old.idpk
        old_payload = deepcopy(old.payload)
        error = SimpleNamespace(cycleId=ctx.cid, reason="REPORT_TOO_EARLY",
                                data=SimpleNamespace(target=old_id,
                                    opensAt=ctx.now + timedelta(minutes=1)))
        process_report_error(session, error, now=ctx.now + timedelta(seconds=1))
        session.commit()
    with Session(engine) as session:
        rows = _rows(session, ctx.cid, "negotiation-report")
        assert len(rows) == 2
        previous, retry = rows
        assert previous.msg_id == old_id and previous.idpk == old_idpk
        assert previous.payload == old_payload
        assert previous.dispatch_required is False
        assert retry.idpk == old_idpk
        assert retry.msg_id != old_id
        assert retry.payload["idpk"] == old_idpk
        assert retry.payload["msgId"] == retry.msg_id
        assert retry.payload["data"] == old_payload["data"]
        assert retry.available_at == ctx.now + timedelta(minutes=1)
        assert session.exec(select(NegotiationReport).where(NegotiationReport.cycle_id == ctx.cid)).one().status == "DEFERRED"


def test_report_error_retry_rollback_reverts_both(report_scenario):
    ctx = report_scenario
    with Session(engine) as session:
        enqueue_due_negotiation_reports(session, city_id=CITY_ID, routing_key="central", now=ctx.now)
        session.commit()
    with Session(engine) as session:
        old = _rows(session, ctx.cid, "negotiation-report")[0]
        error = SimpleNamespace(cycleId=ctx.cid, reason="REPORT_TOO_EARLY",
                                data=SimpleNamespace(target=old.msg_id,
                                    opensAt=ctx.now + timedelta(minutes=1)))
        process_report_error(session, error, now=ctx.now + timedelta(seconds=1))
        session.rollback()
    with Session(engine) as session:
        assert len(_rows(session, ctx.cid, "negotiation-report")) == 1
        assert _rows(session, ctx.cid, "negotiation-report")[0].dispatch_required
        assert session.exec(select(NegotiationReport).where(NegotiationReport.cycle_id == ctx.cid)).one().status == "PENDING"


def test_outbox_relay_retry_preserves_offer_ids_and_payload(offer_scenario):
    ctx = offer_scenario
    with Session(engine) as session:
        assert change_local_offer_price(session, offer_id=ctx.oid, price=Decimal("6.20"))
        session.commit()
    t0 = datetime.now(timezone.utc)
    with Session(engine) as session:
        claim = claim_batch(session, worker_id="relay-e3028", kinds=("BROKER",), now=t0,
                            lease_seconds=30)
        assert len(claim) == 1
        original = (claim[0].id, claim[0].msg_id, claim[0].idpk, deepcopy(claim[0].payload), claim[0].claim_token)
        session.commit()
    with Session(engine) as session:
        assert finish_claim(session, row_id=original[0], claim_token=original[4],
                            outcome="FAILED", error="BROKER_OFFLINE", now=t0 + timedelta(seconds=1))
        session.commit()
    with Session(engine) as session:
        replay = claim_batch(session, worker_id="relay-e3028-2", kinds=("BROKER",),
                             now=t0 + timedelta(seconds=2))
        assert len(replay) == 1
        assert (replay[0].id, replay[0].msg_id, replay[0].idpk, replay[0].payload) == original[:4]
        assert replay[0].claim_token != original[4]
        session.commit()


def test_market_envelope_is_immutable_at_db_level(offer_scenario):
    ctx = offer_scenario
    with Session(engine) as session:
        assert withdraw_local_offer(session, offer_id=ctx.oid)
        session.commit()
    with Session(engine) as session:
        row = _rows(session, ctx.cid, "offer-withdrawn")[0]
        corrupted = deepcopy(row.payload)
        corrupted["msgId"] = str(uuid4())
        row.payload = corrupted
        session.add(row)
        with pytest.raises(DBAPIError, match="OUTBOX_IMMUTABLE_INTENT"):
            session.commit()
        session.rollback()
    with Session(engine) as session:
        restored = _rows(session, ctx.cid, "offer-withdrawn")[0]
        assert restored.msg_id == restored.payload["msgId"]


def test_duplicate_msgid_rejected_without_second_row(offer_scenario):
    ctx = offer_scenario
    with Session(engine) as session:
        assert change_local_offer_price(session, offer_id=ctx.oid, price=Decimal("5.00"))
        session.commit()
    with Session(engine) as session:
        first = _rows(session, ctx.cid, "offer-updated")[0]
        env = deepcopy(first.payload)
        with pytest.raises(IntegrityError):
            enqueue_market_envelope(session, envelope=env, exchange_name="market",
                                    routing_key=f"market.offer.{CITY_ID}",
                                    available_at=ctx.now, expires_at=ctx.now + timedelta(minutes=5))
        session.rollback()
    with Session(engine) as session:
        assert len(_rows(session, ctx.cid, "offer-updated")) == 1
