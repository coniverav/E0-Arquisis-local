import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

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

    async def publish(*args, **kwargs):
        events.append("protocol_ack")

    async def rabbit_ack():
        events.append("rabbit_ack")

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
        )
    )

    assert events == [
        "protocol_ack",
        "rabbit_ack",
    ]


def test_ignored_message_only_acknowledges_rabbit_delivery():
    exchange = AsyncMock()
    message = AsyncMock()

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
        )
    )

    exchange.publish.assert_not_awaited()
    message.ack.assert_awaited_once()

def test_failed_protocol_ack_does_not_ack_rabbit_delivery():
    exchange = AsyncMock()
    message = AsyncMock()

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

    try:
        asyncio.run(
            complete_successful_message(
                handling=handling,
                message=message,
                exchange=exchange,
                city_user_id="city.KLD",
            )
        )
    except RuntimeError:
        pass

    message.ack.assert_not_awaited()