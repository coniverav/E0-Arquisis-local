"""E3-028: validaciones que no necesitan un broker ni llamadas AWS."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock
from uuid import uuid4

import pytest

from app.services import market_outbox

NOW = datetime(2026, 11, 21, 12, tzinfo=timezone.utc)


def env(kind="offer"):
    data = {"offerId": "KLD-demo", "quantity": 50, "pricePerEnergy": 4.25}
    if kind == "offer-updated":
        data = {"offerId": "KLD-demo", "remaining": 20, "pricePerEnergy": 4.25}
    if kind == "offer-withdrawn":
        data = {"offerId": "KLD-demo"}
    if kind == "negotiation-report":
        data = {"budgetBalance": 5000, "energyBalance": 10}
    return {"msgId": str(uuid4()), "idpk": str(uuid4()), "type": kind,
            "cityId": "KLD", "cycleId": "cycle-1",
            "timestamp": NOW.isoformat(), "data": data}


@pytest.mark.parametrize("kind", ["offer", "offer-updated", "offer-withdrawn", "negotiation-report"])
def test_valid_envelope_is_forwarded_unmodified(monkeypatch, kind):
    payload = env(kind)
    original = deepcopy(payload)
    fake = Mock(return_value=object())
    monkeypatch.setattr(market_outbox, "enqueue_broker", fake)
    destination = ("energy.x", "central") if kind == "negotiation-report" else ("market", "market.offer.KLD")
    session = object()
    expires = NOW + timedelta(minutes=3)
    result = market_outbox.enqueue_market_envelope(
        session, envelope=payload, exchange_name=destination[0],
        routing_key=destination[1], available_at=NOW,
        expires_at=expires, now=NOW,
    )
    assert result is fake.return_value
    assert fake.call_args.args == (session,)
    forwarded = fake.call_args.kwargs
    assert forwarded["envelope"] == original
    assert forwarded["envelope"] is not payload
    assert forwarded["envelope"]["msgId"] == original["msgId"]
    assert forwarded["envelope"]["idpk"] == original["idpk"]
    assert forwarded["available_at"] == NOW
    assert forwarded["expires_at"] == expires
    assert payload == original


@pytest.mark.parametrize("kind,exchange,route", [
    ("offer", "energy.x", "market.offer.KLD"),
    ("offer", "market", "market.offer.TAL"),
    ("offer-updated", "market", "other"),
    ("offer-withdrawn", "wrong", "market.offer.KLD"),
    ("negotiation-report", "market", "central"),
    ("negotiation-report", "energy.x", ""),
])
def test_wrong_destination_never_enqueued(monkeypatch, kind, exchange, route):
    fake = Mock()
    monkeypatch.setattr(market_outbox, "enqueue_broker", fake)
    with pytest.raises(ValueError, match="E3028_INVALID"):
        market_outbox.enqueue_market_envelope(
            object(), envelope=env(kind), exchange_name=exchange,
            routing_key=route, available_at=None, expires_at=None,
        )
    fake.assert_not_called()


@pytest.mark.parametrize("kind", ["bid", "bid-accepted", "SAGA_START", "market-closed"])
def test_noncritical_types_do_not_use_this_helper(monkeypatch, kind):
    fake = Mock()
    monkeypatch.setattr(market_outbox, "enqueue_broker", fake)
    with pytest.raises(ValueError, match="UNSUPPORTED"):
        market_outbox.enqueue_market_envelope(
            object(), envelope=env(kind), exchange_name="market",
            routing_key="market.offer.KLD", available_at=None, expires_at=None,
        )
    fake.assert_not_called()


@pytest.mark.parametrize("field", ["msgId", "idpk", "cycleId", "timestamp", "cityId", "data"])
def test_missing_v3_envelope_field_is_rejected(monkeypatch, field):
    fake = Mock()
    monkeypatch.setattr(market_outbox, "enqueue_broker", fake)
    payload = env()
    payload.pop(field)
    with pytest.raises(ValueError):
        market_outbox.enqueue_market_envelope(
            object(), envelope=payload, exchange_name="market",
            routing_key="market.offer.KLD", available_at=None, expires_at=None,
        )
    fake.assert_not_called()


def test_report_requires_balances(monkeypatch):
    fake = Mock()
    monkeypatch.setattr(market_outbox, "enqueue_broker", fake)
    payload = env("negotiation-report")
    payload["data"].pop("energyBalance")
    with pytest.raises(ValueError, match="BALANCES"):
        market_outbox.enqueue_market_envelope(
            object(), envelope=payload, exchange_name="energy.x",
            routing_key="central", available_at=None, expires_at=None,
        )
    fake.assert_not_called()
