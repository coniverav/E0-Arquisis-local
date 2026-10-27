"""Historial y snapshots transaccionales en PostgreSQL temporal."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import os
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlmodel import Session, select

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://test:test@localhost/test")

from app.database import engine, run_migrations
from app.main import app
from app.models import InboundMessage, MarketCycle, MarketEventClaim, MarketFeedOffer


URL = "/internal/audit/inbound/market"


def event(message_type="offer", *, cycle=None, city="HGW", offer_id="offer-1",
          timestamp="2026-10-25T12:00:00Z", **data):
    payload = {
        "idpk": str(uuid4()), "msgId": str(uuid4()),
        "type": message_type, "timestamp": timestamp,
        "cycleId": cycle or f"cycle-{uuid4()}",
    }
    if message_type == "market-closed":
        payload.update(sender="central", data={})
    else:
        values = {"offerId": offer_id}
        if message_type == "offer":
            values.update(quantity=20, pricePerEnergy=330)
        elif message_type == "offer-updated":
            values.update(remaining=12, pricePerEnergy=320)
        values.update(data)
        payload.update(cityId=city, data=values)
    return payload


def post(payload, *, client=None):
    response = (client or TestClient(app)).post(URL, json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def snapshot(cycle, city):
    with Session(engine) as session:
        return session.exec(select(MarketFeedOffer).where(
            MarketFeedOffer.cycle_id == cycle, MarketFeedOffer.city_id == city,
        )).one()


def history(cycle):
    with Session(engine) as session:
        return session.exec(select(InboundMessage).where(
            InboundMessage.cycle_id == cycle,
            InboundMessage.message_type.in_(
                ["offer", "offer-updated", "offer-withdrawn", "market-closed"]
            ),
        ).order_by(InboundMessage.id)).all()


def setup_module():
    run_migrations()


def test_offer_update_withdrawal_keep_history_and_latest_snapshot():
    cycle = f"cycle-{uuid4()}"
    first = event(cycle=cycle)
    first["futureField"] = "preserved"
    assert post(first)["status"] == "PROCESSED"
    stored = snapshot(cycle, "HGW")
    assert (stored.state, stored.initial_quantity, stored.remaining,
            stored.price_per_energy, stored.uncertain) == (
                "ACTIVE", 20, 20, Decimal("330"), False,
            )

    updated = event("offer-updated", cycle=cycle,
                    timestamp="2026-10-25T12:01:00Z", pricePerEnergy=320.07)
    assert post(updated)["status"] == "PROCESSED"
    stored = snapshot(cycle, "HGW")
    assert (stored.remaining, stored.price_per_energy, stored.state) == (
        12, Decimal("320.07"), "ACTIVE",
    )

    withdrawn = event("offer-withdrawn", cycle=cycle,
                      timestamp="2026-10-25T12:02:00Z")
    assert post(withdrawn)["status"] == "PROCESSED"
    stored = snapshot(cycle, "HGW")
    assert (stored.state, stored.remaining) == ("WITHDRAWN", 0)
    rows = history(cycle)
    assert [row.message_type for row in rows] == [
        "offer", "offer-updated", "offer-withdrawn",
    ]
    assert [row.offer_id for row in rows] == ["offer-1"] * 3
    assert rows[0].payload["futureField"] == "preserved"
    assert all(row.processed_at is not None for row in rows)


def test_close_is_cycle_scoped_terminal_and_keeps_history():
    old_cycle, current_cycle = f"cycle-{uuid4()}", f"cycle-{uuid4()}"
    post(event(cycle=old_cycle, city="HGW"))
    post(event(cycle=old_cycle, city="COR", offer_id="cor-offer"))
    post(event(cycle=current_cycle, city="HGW"))
    closed = event("market-closed", cycle=old_cycle,
                   timestamp="2026-10-25T12:05:00Z")
    assert post(closed)["status"] == "PROCESSED"

    with Session(engine) as session:
        old_market = session.get(MarketCycle, old_cycle)
        current_market = session.get(MarketCycle, current_cycle)
        assert old_market.closed_msg_id == closed["msgId"]
        assert current_market.closed_at is None
    assert snapshot(old_cycle, "HGW").state == "CLOSED"
    assert snapshot(old_cycle, "COR").state == "CLOSED"
    assert snapshot(current_cycle, "HGW").state == "ACTIVE"

    late = event("offer-updated", cycle=old_cycle,
                 timestamp="2026-10-25T12:06:00Z")
    assert post(late)["status"] == "NOT_PROCESSED"
    assert snapshot(old_cycle, "HGW").state == "CLOSED"
    assert len(history(old_cycle)) == 4
    assert post(event("market-closed", cycle=old_cycle))["status"] == "NOT_PROCESSED"


def test_idpk_retry_and_conflicting_reuse_do_not_reapply_snapshot():
    cycle = f"cycle-{uuid4()}"
    original = event(cycle=cycle)
    assert post(original)["status"] == "PROCESSED"
    retry = dict(original, msgId=str(uuid4()), timestamp="2026-10-25T12:01:00Z")
    duplicate = post(retry)
    assert duplicate["status"] == "DUPLICATE"
    assert duplicate["relatedMsgId"] == original["msgId"]
    assert post(original)["status"] == "DUPLICATE"

    changed = dict(original, msgId=str(uuid4()), data={
        "offerId": "offer-1", "quantity": 99, "pricePerEnergy": 330,
    })
    assert post(changed)["status"] == "NOT_PROCESSED"
    reused_msg_id = dict(original, idpk=str(uuid4()), data={
        "offerId": "offer-1", "quantity": 98, "pricePerEnergy": 330,
    })
    assert post(reused_msg_id)["status"] == "NOT_PROCESSED"
    assert snapshot(cycle, "HGW").initial_quantity == 20
    assert [row.status for row in history(cycle)] == [
        "PROCESSED", "DUPLICATE", "DUPLICATE", "NOT_PROCESSED", "NOT_PROCESSED",
    ]
    with Session(engine) as session:
        claims = session.exec(select(MarketEventClaim).where(
            MarketEventClaim.sender == "HGW",
            MarketEventClaim.idpk == original["idpk"],
        )).all()
        assert len(claims) == 1


def test_city_and_cycle_are_independent_and_offer_id_conflict_is_visible():
    first_cycle, next_cycle = f"cycle-{uuid4()}", f"cycle-{uuid4()}"
    post(event(cycle=first_cycle, city="HGW", offer_id="shared"))
    post(event(cycle=first_cycle, city="COR", offer_id="shared"))
    post(event(cycle=next_cycle, city="HGW", offer_id="shared"))
    conflicting = event(cycle=first_cycle, city="HGW", offer_id="second-offer",
                        timestamp="2026-10-25T12:01:00Z")
    assert post(conflicting)["status"] == "NOT_PROCESSED"
    assert snapshot(first_cycle, "HGW").offer_id == "shared"
    assert snapshot(first_cycle, "HGW").uncertain is True
    assert snapshot(first_cycle, "COR").state == "ACTIVE"
    assert snapshot(next_cycle, "HGW").state == "ACTIVE"


def test_out_of_order_update_retains_newer_snapshot_and_marks_uncertainty():
    cycle = f"cycle-{uuid4()}"
    update = event("offer-updated", cycle=cycle,
                   timestamp="2026-10-25T12:02:00Z")
    assert post(update)["status"] == "NOT_PROCESSED"
    stored = snapshot(cycle, "HGW")
    assert (stored.state, stored.remaining, stored.uncertain) == (
        "UNRESOLVED", 12, True,
    )
    base = event(cycle=cycle, timestamp="2026-10-25T12:00:00Z")
    assert post(base)["status"] == "PROCESSED"
    stored = snapshot(cycle, "HGW")
    assert (stored.state, stored.initial_quantity, stored.remaining,
            stored.price_per_energy, stored.uncertain) == (
                "ACTIVE", 20, 12, Decimal("320"), True,
            )
    stale = event("offer-updated", cycle=cycle,
                  timestamp="2026-10-25T12:01:00Z",
                  remaining=18, pricePerEnergy=310)
    assert post(stale)["status"] == "NOT_PROCESSED"
    assert snapshot(cycle, "HGW").remaining == 12
    assert len(history(cycle)) == 3


def test_withdrawal_before_offer_never_reopens_snapshot():
    cycle = f"cycle-{uuid4()}"
    assert post(event("offer-withdrawn", cycle=cycle,
                      timestamp="2026-10-25T12:02:00Z"))["status"] == "PROCESSED"
    assert post(event(cycle=cycle, timestamp="2026-10-25T12:00:00Z"))["status"] == "NOT_PROCESSED"
    stored = snapshot(cycle, "HGW")
    assert (stored.state, stored.uncertain) == ("WITHDRAWN", True)


def test_failed_transaction_rolls_back_history_claim_and_snapshot_then_retry_succeeds():
    payload = event()
    with patch("app.services.market_feed._apply_offer_event", side_effect=RuntimeError("db-failure")):
        response = TestClient(app, raise_server_exceptions=False).post(URL, json=payload)
    assert response.status_code == 500
    with Session(engine) as session:
        assert session.exec(select(InboundMessage).where(
            InboundMessage.idpk == payload["idpk"]
        )).first() is None
        assert session.exec(select(MarketEventClaim).where(
            MarketEventClaim.idpk == payload["idpk"]
        )).first() is None
        assert session.get(MarketCycle, payload["cycleId"]) is None
    assert post(payload)["status"] == "PROCESSED"


def test_two_master_requests_serialize_same_event_without_duplicate_effect():
    payload = event()
    barrier = Barrier(2)

    def send():
        barrier.wait(timeout=5)
        return post(payload)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(lambda _: send(), range(2)))

    assert {first["status"], second["status"]} == {"PROCESSED", "DUPLICATE"}
    assert snapshot(payload["cycleId"], "HGW").initial_quantity == 20
    assert len(history(payload["cycleId"])) == 2


def test_concurrent_updates_never_replace_newer_snapshot_with_older_one():
    cycle = f"cycle-{uuid4()}"
    post(event(cycle=cycle))
    older = event("offer-updated", cycle=cycle,
                  timestamp="2026-10-25T12:01:00Z", remaining=12)
    newer = event("offer-updated", cycle=cycle,
                  timestamp="2026-10-25T12:02:00Z", remaining=7)
    barrier = Barrier(2)

    def send(payload):
        barrier.wait(timeout=5)
        return post(payload)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(send, [older, newer]))

    assert second["status"] == "PROCESSED"
    assert first["status"] in {"PROCESSED", "NOT_PROCESSED"}
    assert snapshot(cycle, "HGW").remaining == 7
    assert len(history(cycle)) == 3
