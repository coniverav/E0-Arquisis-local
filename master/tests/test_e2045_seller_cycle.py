"""E2-045: flujo vendedor v3 por connector, HTTP de master y HTTP de wallet.

Ejecutar solo contra PostgreSQL temporal con nombre energyshark_test_*.
El entorno de pruebas necesita dependencias de master y connector.
El broker guarda las publicaciones AMQP en memoria y wallet usa MockTransport.
La prueba no incluye compra automática, liquidación de cierre ni reporte.
"""

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from sqlmodel import Session, select

from app.config import CITY_ID
from app.database import engine
from app.main import app
from app.models import (
    Cycle, InboundMessage, MarketBid, MarketOffer, OfferReservation,
    OutboundMessage, SwapRecord, WalletBalanceSnapshot,
)
from app.services.seller_offer import enqueue_seller_offer
from app.services.seller_swap_dispatch import dispatch_swap_proposals
from app.services.wallet_client import WalletClient
from app.services import wallet_client as wallet_client_module
from tests.contracts_v3 import example, validate


def _iso(value):
    return value.isoformat().replace("+00:00", "Z")


@pytest.fixture
def clock(monkeypatch):
    """Fija las horas de negocio sin esperas ni cambios al reloj del sistema."""
    state = SimpleNamespace(now=datetime.now(timezone.utc).replace(microsecond=0))

    class ControlledDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return state.now if tz is None else state.now.astimezone(tz)

    for module_name in (
        "app.routers.internal_messages", "app.routers.message_audit",
        "app.services.seller_offer", "app.services.seller_swap_dispatch",
        "app.services.seller_bid_accepted", "app.services.seller_bid_acceptance",
        "app.services.seller_offer_events", "app.services.wallet_service",
    ):
        monkeypatch.setattr(importlib.import_module(module_name), "datetime", ControlledDateTime)
    return state


@pytest.fixture
def connector_module(monkeypatch):
    # Importa el connector real con configuración ficticia; no abre conexiones de red.
    connector_dir = Path(__file__).resolve().parents[2] / "connector"
    monkeypatch.syspath_prepend(str(connector_dir))
    for key, value in {
        "RABBITMQ_URL": "amqps://unused.invalid/energy",
        "RABBITMQ_QUEUE": f"city.{CITY_ID}.q",
        "RABBITMQ_MARKET_QUEUE": f"market.{CITY_ID}",
        "RABBITMQ_OUTBOUND_EXCHANGE": "energy.x",
        "RABBITMQ_MARKET_EXCHANGE": "market",
        "RABBITMQ_CENTRAL_ROUTING_KEY": "central",
        "CITY_ID": CITY_ID,
    }.items():
        monkeypatch.setenv(key, value)
    return importlib.import_module("connector")


class RecordingExchange:
    def __init__(self, name):
        self.name = name
        self.published = []

    async def publish(self, message, *, routing_key):
        self.published.append((routing_key, message))


class OneDelivery:
    def __init__(self, message):
        self.message = message
        self.delivered = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.delivered:
            self.delivered = True
            return self.message
        await asyncio.Event().wait()


class LocalBroker:
    """Entrega y publica AMQP en memoria usando el protocolo real del connector."""

    def __init__(self, connector):
        self.connector = connector
        self.directed = RecordingExchange("energy.x")
        self.market = RecordingExchange("market")

    async def deliver_city(self, payload, *, user_id):
        acknowledged = asyncio.Event()
        message = SimpleNamespace(
            body=json.dumps(payload).encode(), user_id=user_id,
            ack=AsyncMock(), reject=AsyncMock(),
        )
        message.ack.side_effect = lambda: acknowledged.set()
        channel = AsyncMock()
        channel.get_queue.return_value = SimpleNamespace(
            iterator=lambda: OneDelivery(message),
        )
        channel.get_exchange.return_value = self.directed
        connection = AsyncMock()
        connection.__aenter__.return_value = connection
        connection.channel.return_value = channel
        real_http_client = httpx.AsyncClient

        def local_backend_client(**kwargs):
            return real_http_client(transport=httpx.ASGITransport(app=app), **kwargs)

        with (
            patch.object(self.connector.aio_pika, "connect", new_callable=AsyncMock,
                         return_value=connection),
            patch.object(self.connector.httpx, "AsyncClient", local_backend_client),
        ):
            task = asyncio.create_task(self.connector.consume_forever())
            try:
                await asyncio.wait_for(acknowledged.wait(), timeout=10)
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        message.ack.assert_awaited_once()
        message.reject.assert_not_awaited()
        channel.get_queue.assert_awaited_once_with(f"city.{CITY_ID}.q", ensure=False)
        channel.get_exchange.assert_awaited_once_with("energy.x", ensure=False)

    async def deliver_market(self, payload, *, user_id):
        """Entrega un evento al consumidor real de market.{CODE}."""
        acknowledged = asyncio.Event()
        message = SimpleNamespace(
            body=json.dumps(payload).encode(), user_id=user_id,
            ack=AsyncMock(), reject=AsyncMock(),
        )
        message.ack.side_effect = lambda: acknowledged.set()
        channel = AsyncMock()
        channel.get_queue.return_value = SimpleNamespace(
            iterator=lambda: OneDelivery(message),
        )
        connection = AsyncMock()
        connection.__aenter__.return_value = connection
        connection.channel.return_value = channel
        real_http_client = httpx.AsyncClient

        def local_backend_client(**kwargs):
            return real_http_client(transport=httpx.ASGITransport(app=app), **kwargs)

        with (
            patch.object(self.connector.aio_pika, "connect", new_callable=AsyncMock,
                         return_value=connection),
            patch.object(self.connector.httpx, "AsyncClient", local_backend_client),
        ):
            task = asyncio.create_task(self.connector.consume_market_forever())
            try:
                await asyncio.wait_for(acknowledged.wait(), timeout=10)
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        message.ack.assert_awaited_once()
        message.reject.assert_not_awaited()
        channel.get_queue.assert_awaited_once_with(f"market.{CITY_ID}", ensure=False)
        channel.get_exchange.assert_not_awaited()

    async def publish_outbox(self, client, *, cycle_id):
        # Ejecuta una consulta acotada a los mismos endpoints HTTP y al publicador
        # que usa publish_backend_outbox(), sin iniciar su bucle permanente.
        response = await client.get(self.connector.MASTER_OUTBOUND_DISPATCH_URL)
        response.raise_for_status()
        published = []
        for item in response.json()["items"]:
            if item["payload"].get("cycleId") != cycle_id:
                continue
            exchange_name, route = self.connector.select_outbound_destination(
                item, city_id=CITY_ID, directed_exchange="energy.x",
                market_exchange="market", central_routing_key="central",
            )
            exchange = self.market if exchange_name == "market" else self.directed
            await self.connector.publish_protocol_message(
                exchange, route, item["payload"], f"city.{CITY_ID}",
            )
            await self.connector.register_outbound_result(
                client, item["msgId"], status="PUBLISHED",
            )
            published.append(item["payload"])
        return published


def _one(session, model, *criteria):
    return session.exec(select(model).where(*criteria)).one()


def test_seller_cycle_from_status_to_wallet_settlement(clock, connector_module, monkeypatch):
    assert (engine.url.database or "").startswith("energyshark_test_")
    broker = LocalBroker(connector_module)
    cycle_id = f"e2045-{uuid4()}"
    offer_id = f"{CITY_ID}-{cycle_id}"
    buyer = "COR" if CITY_ID != "COR" else "HGW"
    swap_id = f"swp-{uuid4().hex}"
    key = ec.generate_private_key(ec.SECP256R1())
    wallet_calls = []
    expected_swap = {}

    def wallet_http(request):
        wallet_calls.append(request)
        assert request.url.host == "wallet.test"
        assert request.headers["X-Api-Key"] == "test-only"
        if request.method == "GET" and request.url.path == "/balance":
            balance = deepcopy(example("wallet-balance-response.json"))
            balance["cityId"] = CITY_ID
            balance["assets"]["KWH"]["available"] = 7300
            balance["assets"]["CR"]["available"] = 0
            return httpx.Response(200, json=balance)
        assert request.method == "POST" and request.url.path == "/swaps"
        assert request.headers["Idempotency-Key"] == expected_swap["idpk"]
        body = json.loads(request.content)
        assert body["memo"] == offer_id
        assert body["give"] == {"asset": "KWH", "amount": float(expected_swap["quantity"])}
        assert body["take"] == {"asset": "CR", "amount": float(expected_swap["amount"])}
        return httpx.Response(202, json={"accepted": True})

    observed = wallet_client_module.ObservedWalletAsyncClient
    transport = httpx.MockTransport(wallet_http)
    monkeypatch.setattr(
        wallet_client_module, "ObservedWalletAsyncClient",
        lambda **kwargs: observed(transport=transport, **kwargs),
    )
    wallet = WalletClient(base_url="https://wallet.test", api_key="test-only")

    status = deepcopy(example("status-statement.json"))
    status.update(idpk=str(uuid4()), msgId=str(uuid4()), cycleId=cycle_id,
                  timestamp=_iso(clock.now))
    status["data"]["energy"].update(
        generationCapacity=20000, consumption=10000, sellable=10000,
    )
    market = status["data"]["market"]
    market.update(
        opensAt=_iso(clock.now - timedelta(minutes=1)),
        lastOfferAt=_iso(clock.now + timedelta(minutes=4, seconds=30)),
        lastBidAt=_iso(clock.now + timedelta(minutes=5)),
        closesAt=_iso(clock.now + timedelta(minutes=6)),
        reportOpensAt=_iso(clock.now + timedelta(minutes=6)),
    )
    status["data"]["validUntil"] = _iso(clock.now + timedelta(minutes=7))
    validate("amqp/status-statement", status)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as backend:
            await broker.deliver_city(status, user_id="central")
            with Session(engine) as session:
                cycle = session.get(Cycle, cycle_id)
                assert cycle.status_idpk == status["idpk"]
                assert cycle.strategy_version is not None

            assert await enqueue_seller_offer(cycle_id, wallet_client=wallet)
            assert not await enqueue_seller_offer(cycle_id, wallet_client=wallet)
            with Session(engine) as session:
                offer = _one(session, MarketOffer, MarketOffer.offer_id == offer_id)
                quantity = market["minBid"]
                assert offer.initial_quantity >= quantity
                price = offer.price_per_energy
            sent = await broker.publish_outbox(backend, cycle_id=cycle_id)
            assert [item["type"] for item in sent] == ["offer"]
            validate("amqp/offer", sent[0])
            assert broker.market.published[0][0] == f"market.offer.{CITY_ID}"
            assert broker.market.published[0][1].user_id == f"city.{CITY_ID}"

            bid = {
                "idpk": str(uuid4()), "msgId": str(uuid4()), "type": "bid",
                "timestamp": _iso(clock.now), "cityId": buyer, "cycleId": cycle_id,
                "data": {"offerId": offer_id, "quantity": quantity},
            }
            await broker.deliver_city(bid, user_id=f"city.{buyer}")
            await broker.deliver_city(bid, user_id=f"city.{buyer}")
            with Session(engine) as session:
                local_bid = _one(session, MarketBid, MarketBid.idpk == bid["idpk"])
                reservation = _one(session, OfferReservation,
                                   OfferReservation.bid_idpk == bid["idpk"])
                record = _one(session, SwapRecord, SwapRecord.idpk == bid["idpk"])
                assert local_bid.status == "RESERVED"
                assert reservation.status == "HOLD"
                assert record.status == "SUBMITTED"
                assert record.idempotency_key == bid["idpk"]
                amount = record.take_amount
                assert amount == (Decimal(quantity) * price).quantize(Decimal("0.01"))
                expected_swap.update(idpk=bid["idpk"], quantity=quantity, amount=amount)
                assert session.exec(select(OutboundMessage).where(
                    OutboundMessage.cycle_id == cycle_id,
                    OutboundMessage.message_type == "bid-accepted",
                )).all() == []

            result = await dispatch_swap_proposals(client=wallet, now=clock.now)
            assert result["accepted_202"] == 1
            assert (await dispatch_swap_proposals(client=wallet, now=clock.now))["claimed"] == 0
            assert [call.method for call in wallet_calls] == ["GET", "POST"]
            with Session(engine) as session:
                record = _one(session, SwapRecord, SwapRecord.idpk == bid["idpk"])
                assert record.status == "SUBMITTED"  # HTTP 202 no confirma la liquidación.
                assert record.proposal_http_accepted_at is not None
                assert _one(session, OfferReservation,
                            OfferReservation.bid_idpk == bid["idpk"]).status == "HOLD"
                assert not session.exec(select(OutboundMessage).where(
                    OutboundMessage.cycle_id == cycle_id,
                    OutboundMessage.message_type == "bid-accepted",
                )).all()

            expires = clock.now + timedelta(seconds=45)
            give = {"asset": "KWH", "amount": quantity}
            take = {"asset": "CR", "amount": float(amount)}
            token = jwt.encode({
                "iss": "wallet.energyshark", "sub": swap_id,
                "iat": int(clock.now.timestamp()), "exp": int(expires.timestamp()),
                "proposer": CITY_ID, "give": give, "take": take, "memo": offer_id,
            }, key, algorithm="ES256")
            proposed = {
                "idpk": str(uuid4()), "msgId": str(uuid4()),
                "type": "swap-proposed", "sender": "central",
                "timestamp": _iso(clock.now),
                "data": {"idpk": bid["idpk"], "swapId": swap_id,
                         "proposer": CITY_ID, "give": give, "take": take,
                         "memo": offer_id, "expiresAt": _iso(expires),
                         "token": token},
            }
            await broker.deliver_city(proposed, user_id="central")
            await broker.deliver_city(proposed, user_id="central")
            with Session(engine) as session:
                accepted = session.exec(select(OutboundMessage).where(
                    OutboundMessage.cycle_id == cycle_id,
                    OutboundMessage.message_type == "bid-accepted",
                )).all()
                assert len(accepted) == 1
                assert accepted[0].target_msg_id == bid["msgId"]
                assert accepted[0].payload["data"]["swapId"] == swap_id
                assert accepted[0].payload["data"]["swapToken"] == token
                assert _one(session, SwapRecord, SwapRecord.idpk == bid["idpk"]).status == "PROPOSED"
                assert _one(session, OfferReservation,
                            OfferReservation.bid_idpk == bid["idpk"]).status == "HOLD"
            sent = await broker.publish_outbox(backend, cycle_id=cycle_id)
            assert [item["type"] for item in sent] == ["bid-accepted"]
            assert broker.directed.published[-1][0] == f"city.{buyer}"
            assert broker.directed.published[-1][1].user_id == f"city.{CITY_ID}"

            settled = deepcopy(example("swap-settled.json"))
            settled.update(idpk=str(uuid4()), msgId=str(uuid4()),
                           timestamp=_iso(clock.now))
            settled["data"].update(
                idpk=bid["idpk"], swapId=swap_id, proposer=CITY_ID,
                acceptor=buyer, give=give, take=take, memo=offer_id,
                settledAt=_iso(clock.now),
                balancesAfter={
                    "KWH": {"available": 7300 - quantity, "held": 0},
                    "CR": {"available": float(amount), "held": 0},
                },
            )
            validate("amqp/swap-settled", settled)
            await broker.deliver_city(settled, user_id="central")
            await broker.deliver_city(settled, user_id="central")
            with Session(engine) as session:
                assert _one(session, SwapRecord, SwapRecord.idpk == bid["idpk"]).status == "SETTLED"
                assert _one(session, OfferReservation,
                            OfferReservation.bid_idpk == bid["idpk"]).status == "COMMITTED"
                offer = _one(session, MarketOffer, MarketOffer.offer_id == offer_id)
                assert offer.remaining_quantity == offer.initial_quantity - quantity
                snapshots = session.exec(select(WalletBalanceSnapshot).where(
                    WalletBalanceSnapshot.cycle_id == cycle_id,
                    WalletBalanceSnapshot.source == "EVENT",
                )).all()
                assert len(snapshots) == 1
                assert snapshots[0].kwh_available == Decimal(7300 - quantity)
                assert snapshots[0].cr_available == amount
                assert len(session.exec(select(MarketBid).where(
                    MarketBid.idpk == bid["idpk"])).all()) == 1
                # Los eventos oficiales de wallet no llevan cycleId en el envelope.
                deliveries = session.exec(select(InboundMessage).where(
                    InboundMessage.idpk == settled["idpk"],
                    InboundMessage.message_type == "swap-settled",
                )).all()
                assert {row.status for row in deliveries} == {"PROCESSED", "DUPLICATE"}
            sent = await broker.publish_outbox(backend, cycle_id=cycle_id)
            assert [item["type"] for item in sent] == ["offer-updated"]
            assert sent[0]["data"]["remaining"] == offer.remaining_quantity
            assert broker.market.published[-1][0] == f"market.offer.{CITY_ID}"
            with Session(engine) as session:
                published = session.exec(select(OutboundMessage).where(
                    OutboundMessage.cycle_id == cycle_id,
                    OutboundMessage.message_type.in_(
                        ["offer", "bid-accepted", "offer-updated"]),
                )).all()
                assert sorted(item.message_type for item in published) == [
                    "bid-accepted", "offer", "offer-updated",
                ]
                assert all(item.status == "PUBLISHED" and not item.dispatch_required
                           for item in published)
            print(
                "E2-045 seller local: offer=PUBLISHED bid=RESERVED "
                "wallet_202=SUBMITTED swap=SETTLED reserva=COMMITTED "
                "offer-updated=PUBLISHED"
            )

    asyncio.run(scenario())
