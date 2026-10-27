"""E2-027: observabilidad de confirmación AMQP y privacidad."""
import asyncio
import json
import logging
import os
from unittest.mock import AsyncMock
from uuid import uuid4

os.environ.setdefault("RABBITMQ_URL", "amqps://test:test@example.org/test")
os.environ.setdefault("RABBITMQ_QUEUE", "city.KLD")
os.environ.setdefault("CITY_ID", "KLD")
os.environ.setdefault("RABBITMQ_OUTBOUND_EXCHANGE", "energy.x")
os.environ.setdefault("RABBITMQ_CENTRAL_ROUTING_KEY", "central")

from observability import ObservedIncomingMessage, record_consumer_connection


def test_city_ack_after_original_and_safe_context(caplog):
    raw = AsyncMock()
    raw.body = json.dumps({
        "type": "bid", "idpk": str(uuid4()), "msgId": str(uuid4()),
        "data": {"token": "TOP-SECRET"}, "X-Api-Key": "TOP-SECRET",
    }).encode()
    msg = ObservedIncomingMessage(raw, "city")
    with caplog.at_level(logging.INFO, logger="connector.observability"):
        asyncio.run(msg.ack())
    raw.ack.assert_awaited_once()
    assert "consumer=city" in caplog.text
    assert "outcome=broker_ack" in caplog.text
    assert "latencyMs=" in caplog.text
    assert "TOP-SECRET" not in caplog.text


def test_market_requeue_is_not_protocol_ack(caplog):
    raw = AsyncMock()
    raw.body = b"not-json"
    msg = ObservedIncomingMessage(raw, "market")
    with caplog.at_level(logging.INFO, logger="connector.observability"):
        asyncio.run(msg.reject(requeue=True))
    raw.reject.assert_awaited_once_with(requeue=True)
    raw.ack.assert_not_awaited()
    assert "consumer=market" in caplog.text
    assert "outcome=broker_reject" in caplog.text
    assert "requeue=True" in caplog.text


def test_reconnection_event_has_no_broker_url(caplog):
    with caplog.at_level(logging.INFO, logger="connector.observability"):
        record_consumer_connection("market", "retrying", "ConnectionError")
    assert "state=retrying" in caplog.text
    assert "errorType=ConnectionError" in caplog.text
    assert "amqps://" not in caplog.text


def test_broker_event_is_associated_with_newrelic_application():
    from unittest.mock import MagicMock, patch
    import observability as obs
    agent = MagicMock()
    with patch.object(obs, "nr_agent", agent):
        obs.record_consumer_connection("city", "connected")
    agent.record_custom_event.assert_called_once()
    assert agent.record_custom_event.call_args.kwargs["application"] is agent.application.return_value


def test_failed_broker_ack_does_not_claim_success(caplog):
    raw = AsyncMock()
    raw.body = b"{}"
    raw.ack.side_effect = OSError("offline")
    msg = ObservedIncomingMessage(raw, "city")
    with caplog.at_level(logging.INFO, logger="connector.observability"):
        try:
            asyncio.run(msg.ack())
            assert False, "expected OSError"
        except OSError:
            pass
    assert "outcome=broker_ack" not in caplog.text
