import asyncio
import json
from unittest.mock import AsyncMock

from protocol.publisher import (
    build_amqp_message,
    build_city_user_id,
    publish_protocol_message,
)

#Verifica que el user_id siga el formato exigido para la ciudad.
def test_build_city_user_id():
    assert build_city_user_id("COR") == "city.COR"

#Verifica que el payload y las propiedades AMQP se construyan correctamente.
def test_build_amqp_message():
    payload = {
        "type": "ack",
        "cityId": "COR",
        "data": {
            "target": "550e8400-e29b-41d4-a716-446655440001",
        },
    }

    message = build_amqp_message(
        payload,
        user_id="city.COR",
    )

    assert message.content_type == "application/json"
    assert message.user_id == "city.COR"
    assert json.loads(message.body.decode("utf-8")) == payload

#Verifica que el mensaje se publique una vez usando el routing key indicado.
def test_publish_protocol_message():
    exchange = AsyncMock()

    payload = {
        "type": "ack",
        "cityId": "COR",
        "data": {
            "target": "550e8400-e29b-41d4-a716-446655440001",
        },
    }

    asyncio.run(
        publish_protocol_message(
            exchange=exchange,
            routing_key="central.test", #Valor ficticio del test
            payload=payload,
            user_id="city.COR",
        )
    )

    exchange.publish.assert_awaited_once()

    message = exchange.publish.await_args.args[0]
    routing_key = exchange.publish.await_args.kwargs["routing_key"]

    assert routing_key == "central.test" #Valor ficticio del test
    assert message.user_id == "city.COR"
    assert json.loads(message.body.decode("utf-8")) == payload