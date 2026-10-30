import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

os.environ.setdefault("RABBITMQ_URL", "amqps://test:test@example.org/test")
os.environ.setdefault("RABBITMQ_QUEUE", "city.KLD.q")
os.environ.setdefault("CITY_ID", "KLD")
os.environ.setdefault("RABBITMQ_OUTBOUND_EXCHANGE", "energy.x")
os.environ.setdefault("RABBITMQ_CENTRAL_ROUTING_KEY", "central")

import connector
from protocol.outbound import routing_key_for_directed_response, select_outbound_destination


def outbound(message_type, data, *, route=None, exchange=None, extra=None):
    payload = {
        "idpk": str(uuid4()),
        "msgId": str(uuid4()),
        "type": message_type,
        "timestamp": "2026-10-24T12:00:00Z",
        "cityId": "KLD",
        "cycleId": "cycle-9431",
        "data": data,
    }
    payload.update(extra or {})
    return {
        "msgId": payload["msgId"],
        "idpk": payload["idpk"],
        "type": message_type,
        "payload": payload,
        "exchange": exchange,
        "routingKey": route,
    }


def select(item):
    return select_outbound_destination(
        item,
        city_id="KLD",
        directed_exchange="energy.x",
        market_exchange="market",
        central_routing_key="central",
    )


@pytest.mark.parametrize("message_type,data", [
    ("offer", {"offerId": "KLD-1", "quantity": 10, "pricePerEnergy": 3.5}),
    ("offer-updated", {"offerId": "KLD-1", "remaining": 5, "pricePerEnergy": 3.5}),
    ("offer-withdrawn", {"offerId": "KLD-1"}),
])
def test_offer_types_use_only_own_market_route(message_type, data):
    item = outbound(message_type, data, route="market.offer.KLD", exchange="market")
    assert select(item) == ("market", "market.offer.KLD")
    for exchange, route in (("energy.x", "market.offer.KLD"), ("market", "market.offer.COR")):
        item.update(exchange=exchange, routingKey=route)
        with pytest.raises(ValueError):
            select(item)


@pytest.mark.parametrize("message_type,data,extra", [
    ("bid", {"offerId": "COR-1", "quantity": 2}, {}),
    ("bid-accepted", {
        "target": str(uuid4()), "offerId": "KLD-1", "quantity": 2,
        "pricePerEnergy": 3.5, "amount": 7, "swapId": "swp-1",
        "swapToken": "token", "expiresAt": "2026-10-24T12:00:30Z",
    }, {}),
    ("bid-rejected", {"target": str(uuid4()), "offerId": "KLD-1"},
     {"reason": "OFFER_WITHDRAWN", "code": 410}),
])
def test_bid_types_use_directed_city_route(message_type, data, extra):
    item = outbound(message_type, data, route="city.COR", exchange="energy.x", extra=extra)
    assert select(item) == ("energy.x", "city.COR")
    item["exchange"] = "market"
    with pytest.raises(ValueError):
        select(item)
    item["exchange"] = None
    with pytest.raises(ValueError):
        select(item)


def test_central_messages_and_responses_keep_directed_exchange():
    request = outbound("request", {"ask": "distance-table"}, route="central", exchange=None)
    request["payload"].pop("cycleId")
    assert select(request) == ("energy.x", "central")
    report = outbound("negotiation-report", {"budgetBalance": -2, "energyBalance": 3}, route="central")
    assert select(report) == ("energy.x", "central")
    ack = outbound("ack", {"target": str(uuid4())}, route="city.COR")
    ack["payload"].pop("cycleId")
    assert select(ack) == ("energy.x", "city.COR")
    nack = outbound("nack", {"target": str(uuid4()), "message": "invalid"},
                    route="city.COR", extra={"reason": "UNKNOWN_TYPE", "code": 400})
    nack["payload"].pop("cycleId")
    assert select(nack) == ("energy.x", "city.COR")
    assert routing_key_for_directed_response(amqp_user_id="city.COR", central_routing_key="central") == "city.COR"
    assert routing_key_for_directed_response(amqp_user_id="central", central_routing_key="central") == "central"
    assert routing_key_for_directed_response(amqp_user_id=None, central_routing_key="central") is None


def test_invalid_contract_fields_identity_and_outbox_metadata_are_not_published():
    item = outbound("offer", {"offerId": "KLD-1", "quantity": 2.5, "pricePerEnergy": 3},
                    route="market.offer.KLD", exchange="market")
    with pytest.raises(ValueError):
        select(item)
    item["payload"]["data"]["quantity"] = 2
    item["payload"]["cityId"] = "COR"
    with pytest.raises(ValueError):
        select(item)
    item["payload"]["cityId"] = "KLD"
    item["payload"]["idpk"] = item["payload"]["msgId"]
    with pytest.raises(ValueError):
        select(item)
    item["payload"]["idpk"] = str(uuid4())
    item["idpk"] = "different-from-payload"
    with pytest.raises(ValueError):
        select(item)


def test_required_v3_fields_and_reason_codes_are_checked_without_schema_files():
    item = outbound("offer", {"offerId": "KLD-1", "quantity": 2},
                    route="market.offer.KLD", exchange="market")
    with pytest.raises(ValueError, match="pricePerEnergy"):
        select(item)
    item["payload"]["data"]["pricePerEnergy"] = 3
    item["payload"]["timestamp"] = "2026-10-24T12:00:00"
    with pytest.raises(ValueError, match="timestamp"):
        select(item)

    rejected = outbound("bid-rejected", {"target": str(uuid4()), "offerId": "KLD-1"},
                        route="city.COR", exchange="energy.x",
                        extra={"reason": "QUANTITY_UNAVAILABLE", "code": 409})
    with pytest.raises(ValueError, match="available"):
        select(rejected)
    rejected["payload"]["data"]["available"] = 1
    rejected["payload"]["code"] = 410
    with pytest.raises(ValueError, match="reason/code"):
        select(rejected)


async def _outbox_retries_on_same_exchange_and_never_declares_topology():
    item = outbound("offer", {"offerId": "KLD-1", "quantity": 2, "pricePerEnergy": 3},
                    route="market.offer.KLD", exchange="market")
    directed = AsyncMock()
    market = AsyncMock()
    market.publish.side_effect = [OSError("temporary"), None]
    channel = AsyncMock()
    channel.get_exchange.side_effect = [directed, market]
    connection = AsyncMock()
    connection.__aenter__.return_value = connection
    connection.channel.return_value = channel
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.get.return_value = SimpleNamespace(
        status_code=200,
        raise_for_status=lambda: None,
        json=lambda: {"items": [item]},
    )
    client.post.return_value = SimpleNamespace(
        status_code=200, raise_for_status=lambda: None,
        json=lambda: {"allowed": True, "retryAfter": 0},
    )
    statuses = []

    async def record(_client, _msg_id, *, status, error=None):
        statuses.append(status)

    with (
        patch.object(connector, "CITY_ID", "KLD"),
        patch.object(connector, "RABBITMQ_OUTBOUND_EXCHANGE", "energy.x"),
        patch.object(connector, "RABBITMQ_MARKET_EXCHANGE", "market"),
        patch.object(connector, "RABBITMQ_CENTRAL_ROUTING_KEY", "central"),
        patch.object(connector.aio_pika, "connect", new_callable=AsyncMock, return_value=connection),
        patch.object(connector.httpx, "AsyncClient", return_value=client),
        patch.object(connector, "register_outbound_result", side_effect=record),
        patch.object(connector.asyncio, "sleep", new_callable=AsyncMock, side_effect=[None, asyncio.CancelledError()]),
    ):
        with pytest.raises(asyncio.CancelledError):
            await connector.publish_backend_outbox()

    assert statuses == ["FAILED", "PUBLISHED"]
    assert market.publish.await_count == 2
    assert directed.publish.await_count == 0
    for call in market.publish.await_args_list:
        assert call.kwargs["routing_key"] == "market.offer.KLD"
        assert call.args[0].user_id == "city.KLD"
        assert json.loads(call.args[0].body)["msgId"] == item["msgId"]
    channel.get_exchange.assert_any_await("energy.x", ensure=False)
    channel.get_exchange.assert_any_await("market", ensure=False)
    channel.declare_exchange.assert_not_awaited()
    channel.declare_queue.assert_not_awaited()


def test_outbox_retries_on_same_exchange_and_never_declares_topology():
    asyncio.run(_outbox_retries_on_same_exchange_and_never_declares_topology())


async def _outbox_publishes_bid_and_request():
    bid = outbound("bid", {"offerId": "COR-1", "quantity": 2},
                   route="city.COR", exchange="energy.x")
    request = outbound("request", {"ask": "status-statement"}, route="central")
    request["payload"].pop("cycleId")
    directed = AsyncMock()
    market = AsyncMock()
    channel = AsyncMock()
    channel.get_exchange.side_effect = [directed, market]
    connection = AsyncMock()
    connection.__aenter__.return_value = connection
    connection.channel.return_value = channel
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.get.return_value = SimpleNamespace(
        status_code=200, raise_for_status=lambda: None,
        json=lambda: {"items": [bid, request]},
    )
    client.post.return_value = SimpleNamespace(
        status_code=200, raise_for_status=lambda: None,
        json=lambda: {"allowed": True, "retryAfter": 0},
    )
    with (
        patch.object(connector, "CITY_ID", "KLD"),
        patch.object(connector, "RABBITMQ_OUTBOUND_EXCHANGE", "energy.x"),
        patch.object(connector, "RABBITMQ_MARKET_EXCHANGE", "market"),
        patch.object(connector, "RABBITMQ_CENTRAL_ROUTING_KEY", "central"),
        patch.object(connector.aio_pika, "connect", new_callable=AsyncMock, return_value=connection),
        patch.object(connector.httpx, "AsyncClient", return_value=client),
        patch.object(connector, "register_outbound_result", new_callable=AsyncMock),
        patch.object(connector.asyncio, "sleep", new_callable=AsyncMock,
                     side_effect=asyncio.CancelledError()),
    ):
        with pytest.raises(asyncio.CancelledError):
            await connector.publish_backend_outbox()
    assert directed.publish.await_count == 2
    assert market.publish.await_count == 0
    assert [call.kwargs["routing_key"] for call in directed.publish.await_args_list] == [
        "city.COR", "central",
    ]
    assert all(call.args[0].user_id == "city.KLD" for call in directed.publish.await_args_list)


def test_outbox_publishes_bid_and_request_on_directed_exchange():
    asyncio.run(_outbox_publishes_bid_and_request())


async def _outbox_defers_market_offer_when_quota_is_full():
    item = outbound("offer", {"offerId": "KLD-1", "quantity": 2, "pricePerEnergy": 3},
                    route="market.offer.KLD", exchange="market")
    directed, market, channel = AsyncMock(), AsyncMock(), AsyncMock()
    channel.get_exchange.side_effect = [directed, market]
    connection = AsyncMock()
    connection.__aenter__.return_value = connection
    connection.channel.return_value = channel
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.get.return_value = SimpleNamespace(
        status_code=200, raise_for_status=lambda: None,
        json=lambda: {"items": [item]},
    )
    client.post.return_value = SimpleNamespace(
        status_code=200, raise_for_status=lambda: None,
        json=lambda: {"allowed": False, "retryAfter": 45},
    )
    with (
        patch.object(connector.aio_pika, "connect", new_callable=AsyncMock, return_value=connection),
        patch.object(connector.httpx, "AsyncClient", return_value=client),
        patch.object(connector.asyncio, "sleep", new_callable=AsyncMock,
                     side_effect=asyncio.CancelledError()),
    ):
        with pytest.raises(asyncio.CancelledError):
            await connector.publish_backend_outbox()
    market.publish.assert_not_awaited()
    directed.publish.assert_not_awaited()
    client.post.assert_awaited_once()


def test_outbox_keeps_offer_pending_when_city_quota_is_full():
    asyncio.run(_outbox_defers_market_offer_when_quota_is_full())


def test_protocol_ack_for_another_city_uses_its_route_and_our_identity():
    ack = outbound("ack", {"target": str(uuid4())}, route="city.COR")
    ack["payload"].pop("cycleId")
    exchange = AsyncMock()
    delivery = AsyncMock()
    client = AsyncMock()
    client.post.return_value = SimpleNamespace(status_code=200, text="")
    handling = SimpleNamespace(action="ack", response=ack["payload"])
    with patch.object(connector, "RABBITMQ_OUTBOUND_EXCHANGE", "energy.x"):
        asyncio.run(connector.complete_successful_message(
            handling=handling, message=delivery, exchange=exchange,
            city_user_id="city.KLD", client=client, routing_key="city.COR",
        ))
    exchange.publish.assert_awaited_once()
    assert exchange.publish.await_args.kwargs["routing_key"] == "city.COR"
    assert exchange.publish.await_args.args[0].user_id == "city.KLD"
    assert client.post.await_args_list[0].kwargs["json"]["exchange"] == "energy.x"
    assert client.post.await_args_list[0].kwargs["json"]["routingKey"] == "city.COR"
    delivery.ack.assert_awaited_once()
