"""Comprueba la ruta durable del outbox sin conectar a PostgreSQL ni RabbitMQ."""

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://test:test@localhost/test")

from fastapi.testclient import TestClient

from app.database import get_session
from app.main import app


def _outbound_payload():
    msg_id = str(uuid4())
    idpk = str(uuid4())
    message = {
        "idpk": idpk,
        "msgId": msg_id,
        "type": "offer",
        "timestamp": "2026-10-24T12:00:00Z",
        "cityId": "KLD",
        "cycleId": "cycle-9431",
        "data": {"offerId": "KLD-9431", "quantity": 10, "pricePerEnergy": 2},
    }
    return {
        "msgId": msg_id,
        "idpk": idpk,
        "type": "offer",
        "cycleId": "cycle-9431",
        "exchange": "market",
        "routingKey": "market.offer.KLD",
        "dispatchRequired": True,
        "payload": message,
    }


def test_enqueue_records_exchange_routing_and_dispatch_intent():
    session = MagicMock()
    session.exec.return_value.first.return_value = None
    app.dependency_overrides[get_session] = lambda: session
    try:
        with patch("app.routers.message_audit.record_outbound_message") as record:
            record.return_value = SimpleNamespace(id=7, status="PENDING")
            result = TestClient(app).post("/internal/audit/outbound", json=_outbound_payload())
    finally:
        app.dependency_overrides.clear()
    assert result.status_code == 200
    assert record.call_args.kwargs["exchange_name"] == "market"
    assert record.call_args.kwargs["routing_key"] == "market.offer.KLD"
    assert record.call_args.kwargs["dispatch_required"] is True


def test_enqueue_rejects_other_city_market_route():
    payload = _outbound_payload()
    payload["routingKey"] = "market.offer.COR"
    app.dependency_overrides[get_session] = lambda: MagicMock()
    try:
        result = TestClient(app).post("/internal/audit/outbound", json=payload)
    finally:
        app.dependency_overrides.clear()
    assert result.status_code == 422


def test_dispatch_repeats_same_exchange_and_routing_key():
    payload = _outbound_payload()
    row = SimpleNamespace(
        msg_id=payload["msgId"], idpk=payload["idpk"], message_type="offer",
        routing_key=payload["routingKey"], exchange_name=payload["exchange"],
        payload=payload["payload"], expires_at=None,
    )
    session = MagicMock()
    session.exec.return_value.all.side_effect = [[], [row], [], [row]]
    app.dependency_overrides[get_session] = lambda: session
    try:
        with (
            patch("app.routers.message_audit.lock_city_ledger"),
            patch("app.routers.message_audit.expire_report_dispatches"),
            patch("app.routers.message_audit.renew_failed_bid_dispatches"),
        ):
            client = TestClient(app)
            first = client.get("/internal/audit/outbound/dispatch")
            retry = client.get("/internal/audit/outbound/dispatch")
    finally:
        app.dependency_overrides.clear()
    assert first.status_code == retry.status_code == 200
    assert first.json()["items"] == retry.json()["items"]
    assert first.json()["items"][0]["exchange"] == "market"
    assert first.json()["items"][0]["routingKey"] == "market.offer.KLD"
