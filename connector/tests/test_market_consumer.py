import asyncio
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

os.environ.setdefault("RABBITMQ_URL", "amqps://test:test@example.org/test")
os.environ.setdefault("RABBITMQ_QUEUE", "city.KLD.q")
os.environ.setdefault("RABBITMQ_MARKET_QUEUE", "market.KLD")
os.environ.setdefault("CITY_ID", "KLD")
os.environ.setdefault("RABBITMQ_OUTBOUND_EXCHANGE", "energy.x")
os.environ.setdefault("RABBITMQ_CENTRAL_ROUTING_KEY", "central")

import connector
from protocol.intake import DiscardMessage
from protocol.market import validate_market_feed


def offer_payload():
    return {
        "idpk": str(uuid4()),
        "msgId": str(uuid4()),
        "type": "offer",
        "timestamp": "2026-10-24T12:00:00Z",
        "cityId": "HGW",
        "cycleId": "cycle-9431",
        "data": {"offerId": "HGW-9431", "quantity": 20, "pricePerEnergy": 330},
    }


def market_closed_payload():
    payload = offer_payload()
    payload.update(type="market-closed", sender="central", data={})
    del payload["cityId"]
    return payload


class MarketValidationTests(unittest.TestCase):
    def test_offer_and_central_close_are_accepted(self):
        validate_market_feed(offer_payload(), amqp_user_id="city.HGW")
        validate_market_feed(market_closed_payload(), amqp_user_id=None)

    def test_bad_identity_and_non_market_type_are_discarded_without_e1_validation(self):
        with self.assertRaises(DiscardMessage):
            validate_market_feed(offer_payload(), amqp_user_id="city.COR")
        payload = offer_payload()
        payload["type"] = "bid"
        with self.assertRaises(DiscardMessage):
            validate_market_feed(payload, amqp_user_id="city.HGW")

    def test_invalid_offer_data_and_closed_event_are_discarded(self):
        missing_quantity = offer_payload()
        missing_quantity["data"].pop("quantity")
        boolean_price = offer_payload()
        boolean_price["data"]["pricePerEnergy"] = True
        closed_with_offer = market_closed_payload()
        closed_with_offer["data"] = {"offerId": "unexpected"}
        for payload, user_id in (
            (missing_quantity, "city.HGW"),
            (boolean_price, "city.HGW"),
            (closed_with_offer, None),
        ):
            with self.subTest(message_type=payload["type"]):
                with self.assertRaises(DiscardMessage):
                    validate_market_feed(payload, amqp_user_id=user_id)


class MarketDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_broker_ack_follows_durable_http_receipt_without_protocol_reply(self):
        payload = offer_payload()
        message = AsyncMock()
        message.body = json.dumps(payload).encode()
        message.user_id = "city.HGW"
        client = AsyncMock()
        events = []

        async def post_to_master(_client, sent, *, url):
            self.assertEqual(sent, payload)
            self.assertEqual(url, connector.MASTER_MARKET_URL)
            events.append("persisted")
            return "ack"

        async def broker_ack():
            events.append("broker_ack")

        message.ack.side_effect = broker_ack
        with patch.object(connector, "forward_to_master", side_effect=post_to_master):
            await connector.process_market_message(message, client)

        self.assertEqual(events, ["persisted", "broker_ack"])
        message.reject.assert_not_awaited()

    async def test_backend_rejection_requeues_valid_feed(self):
        message = AsyncMock()
        message.body = json.dumps(offer_payload()).encode()
        message.user_id = "city.HGW"
        with patch.object(connector, "forward_to_master", new_callable=AsyncMock) as post:
            post.return_value = "drop"  # HTTP 4xx: no entrega durable.
            with patch.object(connector.asyncio, "sleep", new_callable=AsyncMock):
                await connector.process_market_message(message, AsyncMock())
        message.ack.assert_not_awaited()
        message.reject.assert_awaited_once_with(requeue=True)

    async def test_invalid_feed_is_audited_then_rejected_without_protocol_nack(self):
        payload = offer_payload()
        message = AsyncMock()
        message.body = json.dumps(payload).encode()
        message.user_id = "city.COR"
        events = []

        async def audit(_client, sent, *, url):
            self.assertEqual(url, connector.MASTER_AUDIT_URL)
            self.assertEqual(sent["status"], "DISCARDED")
            events.append("audited")
            return "ack"

        async def reject(*, requeue):
            self.assertFalse(requeue)
            events.append("broker_reject")

        message.reject.side_effect = reject
        with patch.object(connector, "forward_to_master", side_effect=audit):
            await connector.process_market_message(message, AsyncMock())
        self.assertEqual(events, ["audited", "broker_reject"])
        message.ack.assert_not_awaited()

    async def test_audit_failure_keeps_invalid_delivery_pending(self):
        message = AsyncMock()
        message.body = b"not-json"
        message.user_id = None
        with patch.object(connector, "forward_to_master", new_callable=AsyncMock) as post:
            post.return_value = "retry"
            with patch.object(connector.asyncio, "sleep", new_callable=AsyncMock):
                await connector.process_market_message(message, AsyncMock())
        message.reject.assert_awaited_once_with(requeue=True)
        message.ack.assert_not_awaited()

    async def test_cancel_during_persistence_leaves_delivery_unconfirmed(self):
        message = AsyncMock()
        message.body = json.dumps(offer_payload()).encode()
        message.user_id = "city.HGW"
        pending = asyncio.Event()

        async def slow_backend(*_args, **_kwargs):
            pending.set()
            await asyncio.Event().wait()

        with patch.object(connector, "forward_to_master", side_effect=slow_backend):
            task = asyncio.create_task(connector.process_market_message(message, AsyncMock()))
            await asyncio.wait_for(pending.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        message.ack.assert_not_awaited()
        message.reject.assert_not_awaited()


class BlockingIterator:
    def __init__(self, entered):
        self.entered = entered
        self.closed = False

    async def __aenter__(self):
        self.entered.set()
        return self

    async def __aexit__(self, *_):
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.Event().wait()


class MarketLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_market_reconnects_without_declaring_topology_and_closes_on_cancel(self):
        entered = asyncio.Event()
        iterator = BlockingIterator(entered)
        queue = SimpleNamespace(iterator=lambda: iterator)
        channel = AsyncMock()
        channel.get_queue.return_value = queue
        connection = AsyncMock()
        connection.__aenter__.return_value = connection
        connection.channel.return_value = channel

        with patch.object(connector.aio_pika, "connect", new_callable=AsyncMock) as connect:
            connect.side_effect = [OSError("disconnect"), connection]
            with patch.object(connector, "CONNECTOR_RETRY_SECONDS", 0):
                task = asyncio.create_task(connector.consume_market_forever())
                try:
                    await asyncio.wait_for(entered.wait(), 2)
                finally:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task

        self.assertEqual(connect.await_count, 2)
        channel.get_queue.assert_awaited_once_with(
            connector.RABBITMQ_MARKET_QUEUE,
            ensure=False,
        )
        channel.set_qos.assert_awaited_once_with(prefetch_count=20)
        self.assertTrue(iterator.closed)
        connection.__aexit__.assert_awaited_once()
        channel.declare_queue.assert_not_awaited()
        channel.declare_exchange.assert_not_awaited()

    async def test_main_starts_independent_workers_and_cancels_all_on_shutdown(self):
        started = set()
        stopped = set()
        all_started = asyncio.Event()

        async def worker(name):
            started.add(name)
            if len(started) == 4:
                all_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.add(name)

        with (
            patch.object(connector, "heartbeat", new=lambda: worker("heartbeat")),
            patch.object(connector, "consume_forever", new=lambda: worker("city")),
            patch.object(connector, "consume_market_forever", new=lambda: worker("market")),
            patch.object(connector, "publish_backend_outbox", new=lambda: worker("outbox")),
        ):
            task = asyncio.create_task(connector.main())
            try:
                await asyncio.wait_for(all_started.wait(), 2)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        self.assertEqual(started, {"heartbeat", "city", "market", "outbox"})
        self.assertEqual(stopped, started)


if __name__ == "__main__":
    unittest.main()
