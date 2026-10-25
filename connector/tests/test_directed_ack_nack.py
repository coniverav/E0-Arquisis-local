"""Entrega dirigida y prevención de respuestas recursivas, sin broker real."""

import asyncio
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

os.environ.setdefault("RABBITMQ_URL", "amqps://test:test@example.org/test")
os.environ.setdefault("RABBITMQ_QUEUE", "city.KLD.q")
os.environ.setdefault("CITY_ID", "KLD")
os.environ.setdefault("RABBITMQ_OUTBOUND_EXCHANGE", "energy.x")
os.environ.setdefault("RABBITMQ_CENTRAL_ROUTING_KEY", "central")

import connector


def message(message_type, **fields):
    payload = {
        "idpk": str(uuid4()),
        "msgId": str(uuid4()),
        "type": message_type,
        "timestamp": "2026-10-25T12:00:00Z",
        "data": {},
    }
    payload.update(fields)
    return payload


def city_bid():
    return message("bid", cityId="COR", cycleId="cycle-1",
                   data={"offerId": "offer-1", "quantity": 5})


def central_distance_table():
    return message("distance-table", sender="central", data={"distances": {}})


class OneMessageIterator:
    def __init__(self, delivery):
        self.delivery = delivery
        self.sent = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.sent:
            self.sent = True
            return self.delivery
        await asyncio.Event().wait()


class DirectedDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def deliver(self, body, user_id):
        done = asyncio.Event()
        events = []
        forwarded = []
        delivery = AsyncMock()
        delivery.body = body
        delivery.user_id = user_id

        async def broker_ack():
            events.append("broker_ack")
            done.set()

        async def broker_reject(*, requeue):
            events.append(("broker_reject", requeue))
            done.set()

        delivery.ack.side_effect = broker_ack
        delivery.reject.side_effect = broker_reject
        queue = SimpleNamespace(iterator=lambda: OneMessageIterator(delivery))
        exchange = AsyncMock()

        async def publish(*_, **__):
            events.append("protocol_publish")

        exchange.publish.side_effect = publish
        channel = AsyncMock()
        channel.get_queue.return_value = queue
        channel.get_exchange.return_value = exchange
        connection = AsyncMock()
        connection.__aenter__.return_value = connection
        connection.channel.return_value = channel

        async def forward(_client, payload, url=connector.MASTER_URL):
            forwarded.append((url, payload))
            events.append(("http", url))
            return "ack"

        with patch.object(connector.aio_pika, "connect", new_callable=AsyncMock,
                          return_value=connection), \
             patch.object(connector, "forward_to_master", side_effect=forward):
            task = asyncio.create_task(connector.consume_forever())
            try:
                await asyncio.wait_for(done.wait(), 2)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        channel.get_exchange.assert_awaited_once_with("energy.x", ensure=False)
        return delivery, exchange, forwarded, events

    async def test_valid_city_message_ack_is_correlated_and_routed_to_city(self):
        original = city_bid()
        delivery, exchange, forwarded, events = await self.deliver(
            json.dumps(original).encode(), "city.COR",
        )
        sent = exchange.publish.await_args.args[0]
        response = json.loads(sent.body)
        self.assertEqual([url for url, _ in forwarded if url == connector.MASTER_URL],
                         [connector.MASTER_URL])
        self.assertEqual(exchange.publish.await_args.kwargs["routing_key"], "city.COR")
        self.assertEqual((response["type"], response["cityId"]), ("ack", "KLD"))
        self.assertEqual(response["data"]["target"], original["msgId"])
        self.assertNotEqual(response["msgId"], original["msgId"])
        self.assertNotEqual(response["idpk"], response["msgId"])
        self.assertEqual(sent.user_id, "city.KLD")
        self.assertLess(events.index("protocol_publish"), events.index("broker_ack"))
        delivery.ack.assert_awaited_once()

    async def test_central_message_ack_is_correlated_and_routed_to_central(self):
        original = central_distance_table()
        delivery, exchange, _, events = await self.deliver(
            json.dumps(original).encode(), None,
        )
        response = json.loads(exchange.publish.await_args.args[0].body)
        self.assertEqual(exchange.publish.await_args.kwargs["routing_key"], "central")
        self.assertEqual(response["data"]["target"], original["msgId"])
        self.assertEqual(events[-1], "broker_ack")
        delivery.ack.assert_awaited_once()

    async def test_invalid_city_message_nack_uses_official_reason_without_business(self):
        original = city_bid()
        original["data"].pop("quantity")
        delivery, exchange, forwarded, events = await self.deliver(
            json.dumps(original).encode(), "city.COR",
        )
        response = json.loads(exchange.publish.await_args.args[0].body)
        self.assertEqual(exchange.publish.await_args.kwargs["routing_key"], "city.COR")
        self.assertEqual((response["type"], response["reason"], response["code"]),
                         ("nack", "MALFORMED_MESSAGE", 422))
        self.assertEqual(response["data"]["target"], original["msgId"])
        self.assertEqual([url for url, _ in forwarded if url == connector.MASTER_URL], [])
        self.assertEqual(forwarded[0][1]["status"], "NACKED")
        self.assertLess(events.index("protocol_publish"), events.index("broker_ack"))
        delivery.ack.assert_awaited_once()

    async def test_wrong_city_identity_nack_returns_to_amqp_sender(self):
        original = city_bid()
        _, exchange, forwarded, _ = await self.deliver(
            json.dumps(original).encode(), "city.HGW",
        )
        response = json.loads(exchange.publish.await_args.args[0].body)
        self.assertEqual(exchange.publish.await_args.kwargs["routing_key"], "city.HGW")
        self.assertEqual((response["reason"], response["code"]),
                         ("IDENTITY_MISMATCH", 403))
        self.assertEqual(response["data"]["target"], original["msgId"])
        self.assertEqual(forwarded[0][1]["status"], "NACKED")

    async def test_received_ack_nack_and_error_never_publish_reply(self):
        target = str(uuid4())
        cases = (
            (message("ack", cityId="COR", data={"target": target}),
             "city.COR", connector.MASTER_URL),
            (message("nack", cityId="COR", reason="MALFORMED_MESSAGE", code=422,
                     data={"target": target, "message": "invalid"}),
             "city.COR", connector.MASTER_URL),
            (message("error", sender="central", cycleId="cycle-1",
                     reason="CYCLE_UNKNOWN", code=404,
                     data={"target": target, "message": "unknown"}),
             None, connector.MASTER_ERROR_URL),
        )
        for original, user_id, expected_url in cases:
            with self.subTest(message_type=original["type"]):
                delivery, exchange, forwarded, _ = await self.deliver(
                    json.dumps(original).encode(), user_id,
                )
                self.assertEqual(forwarded, [(expected_url, original)])
                exchange.publish.assert_not_awaited()
                delivery.ack.assert_awaited_once()

    async def test_malformed_protocol_response_is_audited_without_reply(self):
        original = message("nack", cityId="COR", data={})
        original.pop("idpk")
        delivery, exchange, forwarded, _ = await self.deliver(
            json.dumps(original).encode(), "city.COR",
        )
        self.assertEqual([url for url, _ in forwarded], [connector.MASTER_AUDIT_URL])
        self.assertEqual(forwarded[0][1]["status"], "DISCARDED")
        exchange.publish.assert_not_awaited()
        delivery.reject.assert_awaited_once_with(requeue=False)

    async def test_unparseable_or_uncorrelatable_delivery_is_audited_without_reply(self):
        missing_id = city_bid()
        missing_id.pop("msgId")
        for body in (b"not-json", json.dumps(missing_id).encode()):
            with self.subTest(body=body):
                delivery, exchange, forwarded, _ = await self.deliver(body, "city.COR")
                self.assertEqual([url for url, _ in forwarded], [connector.MASTER_AUDIT_URL])
                self.assertEqual(forwarded[0][1]["status"], "DISCARDED")
                exchange.publish.assert_not_awaited()
                delivery.reject.assert_awaited_once_with(requeue=False)

    async def test_market_feed_never_invokes_protocol_publisher(self):
        original = message("offer", cityId="COR", cycleId="cycle-1",
                           data={"offerId": "offer-1", "quantity": 5, "pricePerEnergy": 100})
        delivery = AsyncMock()
        delivery.body = json.dumps(original).encode()
        delivery.user_id = "city.COR"
        with patch.object(connector, "forward_to_master", new_callable=AsyncMock,
                          return_value="ack") as forward, \
             patch.object(connector, "dispatch_protocol_response", new_callable=AsyncMock) as dispatch:
            await connector.process_market_message(delivery, AsyncMock())
        self.assertEqual(forward.await_args.kwargs["url"], connector.MASTER_MARKET_URL)
        dispatch.assert_not_awaited()
        delivery.ack.assert_awaited_once()
