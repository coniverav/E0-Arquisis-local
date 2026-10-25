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
from protocol.handler import plan_protocol_response
from protocol.intake import DiscardMessage, decode_incoming_payload


def bid_payload():
    return {
        "idpk": str(uuid4()),
        "msgId": str(uuid4()),
        "type": "bid",
        "timestamp": "2026-10-24T12:00:00Z",
        "cityId": "COR",
        "cycleId": "cycle-9431",
        "data": {"offerId": "offer-1", "quantity": 5},
    }


def wallet_payload():
    return {
        "idpk": str(uuid4()),
        "msgId": str(uuid4()),
        "type": "swap-proposed",
        "timestamp": "2026-10-24T12:00:00Z",
        "sender": "central",
        "data": {
            "swapId": "swp-77",
            "idpk": str(uuid4()),
            "proposer": "HGW",
            "give": {"asset": "KWH", "amount": 7300},
            "take": {"asset": "CR", "amount": 2409000},
            "memo": "HGW-9431",
            "expiresAt": "2026-10-24T12:00:42Z",
            "token": "eyJhbGciOiJFUzI1NiIs...",
            "balancesAfter": {
                "CR": {"available": 1800000, "held": 0},
                "KWH": {"available": 12700, "held": 7300},
            },
        },
    }


class IdentityPlanningTests(unittest.TestCase):
    def test_matching_city_bid_is_acknowledged(self):
        payload = bid_payload()
        result = plan_protocol_response(payload, "KLD", amqp_user_id="city.COR")
        self.assertEqual(result.action, "ack")
        self.assertEqual(result.response["data"]["target"], payload["msgId"])

    def test_forged_city_bid_gets_identity_nack(self):
        payload = bid_payload()
        result = plan_protocol_response(payload, "KLD", amqp_user_id="city.HGW")
        self.assertEqual(result.action, "nack")
        self.assertEqual((result.response["reason"], result.response["code"]),
                         ("IDENTITY_MISMATCH", 403))
        self.assertEqual(result.response["data"]["target"], payload["msgId"])
        self.assertEqual(result.response["cityId"], "KLD")

    def test_parseable_invalid_bid_gets_malformed_nack(self):
        payload = bid_payload()
        payload["data"] = {"offerId": "offer-1"}
        result = plan_protocol_response(payload, "KLD", amqp_user_id="city.COR")
        self.assertEqual(result.action, "nack")
        self.assertEqual((result.response["reason"], result.response["code"]),
                         ("MALFORMED_MESSAGE", 422))
        self.assertEqual(result.response["data"]["target"], payload["msgId"])

    def test_unparseable_and_uncorrelatable_messages_are_discarded(self):
        with self.assertRaises(DiscardMessage):
            decode_incoming_payload(b"not json")
        payload = bid_payload()
        payload.pop("msgId")
        with self.assertRaises(DiscardMessage):
            decode_incoming_payload(json.dumps(payload).encode())
        self.assertEqual(plan_protocol_response(payload, "KLD", amqp_user_id="city.COR").action,
                         "discard")

    def test_central_and_wallet_event_do_not_require_city_user_id(self):
        central = bid_payload()
        central.update(type="distance-table", sender="central", data={"distances": {}})
        central.pop("cityId")
        for payload in (central, wallet_payload()):
            result = plan_protocol_response(payload, "KLD", amqp_user_id=None)
            self.assertEqual(result.action, "ack")

    def test_city_cannot_claim_central_sender(self):
        payload = bid_payload()
        payload.pop("cityId")
        payload["sender"] = "central"
        result = plan_protocol_response(payload, "KLD", amqp_user_id="city.COR")
        self.assertEqual(result.action, "nack")
        self.assertEqual(result.response["reason"], "MALFORMED_MESSAGE")


class OneMessageIterator:
    def __init__(self, message):
        self.message = message
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
            return self.message
        await asyncio.Event().wait()


class CityDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def deliver(self, body, user_id):
        done = asyncio.Event()
        delivery = AsyncMock()
        delivery.body = body
        delivery.user_id = user_id
        delivery.ack.side_effect = lambda: done.set()
        delivery.reject.side_effect = lambda **_: done.set()
        queue = SimpleNamespace(iterator=lambda: OneMessageIterator(delivery))
        channel = AsyncMock()
        channel.get_queue.return_value = queue
        connection = AsyncMock()
        connection.__aenter__.return_value = connection
        connection.channel.return_value = channel
        forwarded = []

        async def forward(_client, payload, url=connector.MASTER_URL):
            forwarded.append((url, payload))
            return "ack"

        with patch.object(connector.aio_pika, "connect", new_callable=AsyncMock,
                          return_value=connection), \
             patch.object(connector, "forward_to_master", side_effect=forward), \
             patch.object(connector, "register_outbound_pending", new_callable=AsyncMock) as pending, \
             patch.object(connector, "dispatch_protocol_response", new_callable=AsyncMock) as dispatch, \
             patch.object(connector, "register_outbound_result", new_callable=AsyncMock):
            task = asyncio.create_task(connector.consume_forever())
            try:
                await asyncio.wait_for(done.wait(), 2)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        return delivery, forwarded, pending, dispatch

    async def test_forged_bid_is_audited_and_nacked_to_authenticated_city_only(self):
        payload = bid_payload()
        delivery, forwarded, pending, dispatch = await self.deliver(
            json.dumps(payload).encode(), "city.HGW",
        )
        self.assertEqual([url for url, _ in forwarded], [connector.MASTER_AUDIT_URL])
        audit = forwarded[0][1]
        self.assertEqual((audit["status"], audit["reasonCode"]),
                         ("NACKED", "IDENTITY_MISMATCH"))
        self.assertIn("user_id='city.HGW'", audit["reason"])
        self.assertEqual(audit["msgId"], payload["msgId"])
        self.assertEqual(dispatch.await_args.kwargs["routing_key"], "city.HGW")
        self.assertEqual(dispatch.await_args.kwargs["result"].response["data"]["target"],
                         payload["msgId"])
        pending.assert_awaited_once()
        delivery.ack.assert_awaited_once()
        delivery.reject.assert_not_awaited()

    async def test_missing_user_id_is_discarded_without_unsafe_reply_or_business(self):
        delivery, forwarded, pending, dispatch = await self.deliver(
            json.dumps(bid_payload()).encode(), None,
        )
        self.assertEqual([url for url, _ in forwarded], [connector.MASTER_AUDIT_URL])
        self.assertEqual(forwarded[0][1]["status"], "DISCARDED")
        self.assertIn("Sin ruta AMQP autenticada", forwarded[0][1]["reason"])
        pending.assert_not_awaited()
        dispatch.assert_not_awaited()
        delivery.reject.assert_awaited_once_with(requeue=False)

    async def test_malformed_bid_is_nacked_without_reaching_business(self):
        payload = bid_payload()
        payload["data"].pop("quantity")
        delivery, forwarded, _pending, dispatch = await self.deliver(
            json.dumps(payload).encode(), "city.COR",
        )
        self.assertEqual([url for url, _ in forwarded], [connector.MASTER_AUDIT_URL])
        self.assertEqual((forwarded[0][1]["status"], forwarded[0][1]["reasonCode"]),
                         ("NACKED", "MALFORMED_MESSAGE"))
        self.assertEqual(dispatch.await_args.kwargs["routing_key"], "city.COR")
        delivery.ack.assert_awaited_once()

    async def test_bad_json_is_audited_without_protocol_reply(self):
        delivery, forwarded, pending, dispatch = await self.deliver(b"not json", "city.COR")
        self.assertEqual([url for url, _ in forwarded], [connector.MASTER_AUDIT_URL])
        self.assertEqual(forwarded[0][1]["status"], "DISCARDED")
        pending.assert_not_awaited()
        dispatch.assert_not_awaited()
        delivery.reject.assert_awaited_once_with(requeue=False)

    async def test_missing_msgid_is_audited_without_protocol_reply(self):
        payload = bid_payload()
        payload.pop("msgId")
        delivery, forwarded, pending, dispatch = await self.deliver(
            json.dumps(payload).encode(), "city.COR",
        )
        self.assertEqual([url for url, _ in forwarded], [connector.MASTER_AUDIT_URL])
        self.assertEqual(forwarded[0][1]["status"], "DISCARDED")
        self.assertIsNone(forwarded[0][1]["msgId"])
        pending.assert_not_awaited()
        dispatch.assert_not_awaited()
        delivery.reject.assert_awaited_once_with(requeue=False)

    async def test_valid_bid_reaches_normal_backend_path_and_acks_city(self):
        payload = bid_payload()
        delivery, forwarded, _pending, dispatch = await self.deliver(
            json.dumps(payload).encode(), "city.COR",
        )
        self.assertEqual([url for url, _ in forwarded], [connector.MASTER_URL])
        self.assertEqual(dispatch.await_args.kwargs["routing_key"], "city.COR")
        self.assertEqual(dispatch.await_args.kwargs["result"].response["type"], "ack")
        delivery.ack.assert_awaited_once()

    async def test_wallet_event_uses_central_route(self):
        payload = wallet_payload()
        _delivery, forwarded, _pending, dispatch = await self.deliver(
            json.dumps(payload).encode(), None,
        )
        self.assertEqual([url for url, _ in forwarded], [connector.MASTER_URL])
        self.assertEqual(dispatch.await_args.kwargs["routing_key"], "central")
