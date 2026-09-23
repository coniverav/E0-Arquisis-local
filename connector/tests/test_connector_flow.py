import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

os.environ.setdefault(
    "RABBITMQ_URL",
    "amqps://test:test@example.org/test",
)
os.environ.setdefault("RABBITMQ_QUEUE", "test.queue")
os.environ.setdefault("CITY_ID", "KLD")
os.environ.setdefault(
    "RABBITMQ_OUTBOUND_EXCHANGE",
    "energy.x",
)
os.environ.setdefault(
    "RABBITMQ_CENTRAL_ROUTING_KEY",
    "central",
)

from connector import complete_successful_message


def test_successful_message_publishes_protocol_ack_before_rabbit_ack():
    events = []

    exchange = AsyncMock()
    message = AsyncMock()
    client = AsyncMock()

    async def post(url, json):
        if url.endswith("/internal/audit/outbound"):
            events.append("pending")
        elif json.get("status") == "PUBLISHED":
            events.append("published")

        return SimpleNamespace(
            status_code=200,
            text="",
        )

    async def publish(*args, **kwargs):
        events.append("protocol_ack")

    async def rabbit_ack():
        events.append("rabbit_ack")

    client.post.side_effect = post
    exchange.publish.side_effect = publish
    message.ack.side_effect = rabbit_ack

    handling = SimpleNamespace(
        action="ack",
        response={
            "idpk": "550e8400-e29b-41d4-a716-446655440010",
            "msgId": "550e8400-e29b-41d4-a716-446655440011",
            "type": "ack",
            "timestamp": "2026-09-23T03:00:00+00:00",
            "cityId": "KLD",
            "data": {
                "target": "550e8400-e29b-41d4-a716-446655440001",
            },
        },
    )

    asyncio.run(
        complete_successful_message(
            handling=handling,
            message=message,
            exchange=exchange,
            city_user_id="city.KLD",
            client=client,
        )
    )

    assert events == [
        "pending",
        "protocol_ack",
        "published",
        "rabbit_ack",
    ]

    assert client.post.await_count == 2

def test_ignored_message_only_acknowledges_rabbit_delivery():
    exchange = AsyncMock()
    message = AsyncMock()
    client = AsyncMock()
    client.post.return_value = SimpleNamespace(
        status_code=200,
        text="",
    )

    handling = SimpleNamespace(
        action="ignore",
        response=None,
    )

    asyncio.run(
        complete_successful_message(
            handling=handling,
            message=message,
            exchange=exchange,
            city_user_id="city.KLD",
            client=client,
        )
    )

    exchange.publish.assert_not_awaited()
    message.ack.assert_awaited_once()
    client.post.assert_not_awaited()

def test_failed_protocol_ack_does_not_ack_rabbit_delivery():
    exchange = AsyncMock()
    message = AsyncMock()
    client = AsyncMock()
    audit_payloads = []

    async def post(url, json):
        audit_payloads.append((url, json))

        return SimpleNamespace(
            status_code=200,
            text="",
        )

    client.post.side_effect = post

    exchange.publish.side_effect = RuntimeError(
        "broker publish failed"
    )

    handling = SimpleNamespace(
        action="ack",
        response={
            "idpk": "550e8400-e29b-41d4-a716-446655440010",
            "msgId": "550e8400-e29b-41d4-a716-446655440011",
            "type": "ack",
            "timestamp": "2026-09-23T03:00:00+00:00",
            "cityId": "KLD",
            "data": {
                "target": "550e8400-e29b-41d4-a716-446655440001",
            },
        },
    )

    with pytest.raises(RuntimeError, match="broker publish failed"):
        asyncio.run(
            complete_successful_message(
                handling=handling,
                message=message,
                exchange=exchange,
                city_user_id="city.KLD",
                client=client,
            )
        )

    message.ack.assert_not_awaited()

    assert len(audit_payloads) == 2

    pending_url, pending_payload = audit_payloads[0]
    failed_url, failed_payload = audit_payloads[1]

    assert pending_url.endswith("/internal/audit/outbound")
    assert pending_payload["type"] == "ack"

    assert failed_url.endswith(
        "/550e8400-e29b-41d4-a716-446655440011/result"
    )

    assert failed_payload == {
        "status": "FAILED",
        "error": "broker publish failed",
    }
