import asyncio
import json
from unittest.mock import AsyncMock
from protocol.dispatch import dispatch_protocol_response
from protocol.handler import ProtocolHandlingResult

#Una respuesta preparada debe publicarse en el destino indicado.
def test_dispatch_protocol_response():
    exchange = AsyncMock()

    response = {
        "idpk": "550e8400-e29b-41d4-a716-446655440010",
        "msgId": "550e8400-e29b-41d4-a716-446655440011",
        "type": "nack",
        "timestamp": "2026-09-15T21:00:00Z",
        "cityId": "COR",
        "reason": "UNKNOWN_TYPE",
        "code": 400,
        "data": {
            "target": "550e8400-e29b-41d4-a716-446655440001",
            "message": "Tipo desconocido",
        },
    }

    result = ProtocolHandlingResult(
        action="nack",
        response=response,
    )

    published = asyncio.run(
        dispatch_protocol_response(
            result=result,
            exchange=exchange,
            routing_key="central.test",
            user_id="city.COR",
        )
    )

    assert published is True
    exchange.publish.assert_awaited_once()

    message = exchange.publish.await_args.args[0]

    assert message.user_id == "city.COR"
    assert json.loads(message.body.decode("utf-8")) == response

#discard/ignore no deben producir una publicación.
def test_dispatch_without_response_does_not_publish():
    exchange = AsyncMock()

    result = ProtocolHandlingResult(
        action="discard",
    )

    published = asyncio.run(
        dispatch_protocol_response(
            result=result,
            exchange=exchange,
            routing_key="central.test",
            user_id="city.COR",
        )
    )

    assert published is False
    exchange.publish.assert_not_awaited()