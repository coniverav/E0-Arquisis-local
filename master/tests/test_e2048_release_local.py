"""Ensayo local de Central -> Connector -> Master, sin broker externo."""

import asyncio
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
from sqlmodel import Session, select

from app.database import engine
from app.main import app
from app.models import Cycle, DistanceTable, InboundMessage, LedgerEntry, OutboundMessage
from app.services.cycle_scheduler import process_cycle_windows
from app.services.wallet_client import WalletBalanceResponse, WalletStatementPage
from app.services.wallet_reconciliation import reconcile_wallet
from app.services.wallet_service import WalletService
from tests.contracts_v3 import validate


CONNECTOR_DIR = Path(__file__).resolve().parents[2] / "connector"


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


def _connector(monkeypatch):
    monkeypatch.setenv("RABBITMQ_URL", "amqps://test:test@example.invalid/energy")
    monkeypatch.setenv("RABBITMQ_QUEUE", "city.KLD.q")
    monkeypatch.setenv("RABBITMQ_OUTBOUND_EXCHANGE", "energy.x")
    monkeypatch.setenv("RABBITMQ_CENTRAL_ROUTING_KEY", "central")
    monkeypatch.setenv("CITY_ID", "KLD")
    monkeypatch.syspath_prepend(str(CONNECTOR_DIR))
    spec = importlib.util.spec_from_file_location("e2048_connector", CONNECTOR_DIR / "connector.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _deliver(connector, payload, user_id):
    done = asyncio.Event()
    delivery = AsyncMock()
    delivery.body = json.dumps(payload).encode()
    delivery.user_id = user_id
    delivery.ack.side_effect = lambda: done.set()
    delivery.reject.side_effect = lambda **_: done.set()

    queue = SimpleNamespace(iterator=lambda: OneMessageIterator(delivery))
    exchange = AsyncMock()
    channel = AsyncMock()
    channel.get_queue.return_value = queue
    channel.get_exchange.return_value = exchange
    connection = AsyncMock()
    connection.__aenter__.return_value = connection
    connection.channel.return_value = channel

    original_forward = connector.forward_to_master

    async def forward(_client, body, url=connector.MASTER_URL):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as local_client:
            return await original_forward(local_client, body, url)

    with patch.object(connector.aio_pika, "connect", new_callable=AsyncMock,
                      return_value=connection), patch.object(connector, "forward_to_master", side_effect=forward):
        task = asyncio.create_task(connector.consume_forever())
        try:
            await asyncio.wait_for(done.wait(), 5)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    return delivery, exchange


def _envelope(message_type, data, *, cycle_id=None, idpk=None, msg_id=None):
    payload = {
        "idpk": idpk or str(uuid4()),
        "msgId": msg_id or str(uuid4()),
        "type": message_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "sender": "central",
        "data": data,
    }
    if cycle_id is not None:
        payload["cycleId"] = cycle_id
    return payload


def test_central_cycle_is_processed_and_replays_do_not_duplicate_effects(monkeypatch):
    connector = _connector(monkeypatch)
    cycle_id = f"cycle-e2048-{uuid4()}"
    now = datetime.now(timezone.utc)
    iso = lambda minutes: (now + timedelta(minutes=minutes)).isoformat()
    status = _envelope("status-statement", {
        "energy": {"generationCapacity": 900, "consumption": 1000,
                   "generationCost": 200, "sellable": 0},
        "market": {"buybackPrice": 210, "imbalancePrice": 600,
                   "minBid": 1, "opensAt": iso(1), "lastOfferAt": iso(7),
                   "lastBidAt": iso(8), "closesAt": iso(10),
                   "reportOpensAt": iso(10),
                   "limits": {"marketOpsPerMinute": 60,
                              "walletWritesPerMinute": 120,
                              "walletReadsPerMinute": 300}},
        "validUntil": iso(15),
    }, cycle_id=cycle_id)
    transfer = _envelope("transfer", {
        "quantity": 120, "balanceAfter": 120, "creditLine": 0,
    }, cycle_id=cycle_id)
    distance = _envelope("distance-table", {"distances": {
        "HGW": {"distance": 100, "transportCost": 10, "enabled": True},
    }})
    validate("amqp/status-statement", status)

    async def scenario():
        for payload in (status, transfer, distance):
            delivery, exchange = await _deliver(connector, payload, "central")
            delivery.ack.assert_awaited_once()
            exchange.publish.assert_awaited_once()
            published = json.loads(exchange.publish.await_args.args[0].body)
            assert exchange.publish.await_args.kwargs["routing_key"] == "central"
            assert published["type"] == "ack"
            assert published["data"]["target"] == payload["msgId"]

        # El mismo idpk con otro msgId conserva un solo efecto por mensaje.
        replays = []
        for original in (status, transfer, distance):
            replay = dict(original, msgId=str(uuid4()))
            replay_delivery, replay_exchange = await _deliver(connector, replay, "central")
            replay_delivery.ack.assert_awaited_once()
            replay_exchange.publish.assert_awaited_once()
            replays.append(replay)

        # Un emisor de ciudad que se declara Central nunca llega al negocio.
        forged = _envelope("status-statement", status["data"], cycle_id=f"forged-{uuid4()}")
        forged_delivery, forged_exchange = await _deliver(connector, forged, "city.HGW")
        forged_delivery.ack.assert_awaited_once()
        forged_response = json.loads(forged_exchange.publish.await_args.args[0].body)
        assert forged_exchange.publish.await_args.kwargs["routing_key"] == "city.HGW"
        assert forged_response["type"] == "nack"
        return replays, forged

    replays, forged = asyncio.run(scenario())

    with Session(engine) as session:
        cycle = session.get(Cycle, cycle_id)
        assert cycle.status_msg_id == status["msgId"]
        assert cycle.scheduler_state == "PENDING"
        assert cycle.valid_until == now + timedelta(minutes=15)
        assert cycle.market_opens_at == now + timedelta(minutes=1)
        assert cycle.report_window_opens_at is not None
        assert cycle.budget_balance == Decimal("120")
        assert session.get(Cycle, forged["cycleId"]) is None

        table = session.exec(select(DistanceTable).where(DistanceTable.idpk == distance["idpk"])).one()
        assert table.distances["HGW"]["transportCost"] == 10
        rows = session.exec(select(InboundMessage).where(InboundMessage.msg_id.in_([
            status["msgId"], transfer["msgId"], distance["msgId"],
            *(replay["msgId"] for replay in replays), forged["msgId"],
        ]))).all()
        assert {row.msg_id: row.status for row in rows} == {
            status["msgId"]: "PROCESSED",
            transfer["msgId"]: "PROCESSED",
            distance["msgId"]: "PROCESSED",
            **{replay["msgId"]: "DUPLICATE" for replay in replays},
            forged["msgId"]: "NACKED",
        }
        assert len(session.exec(select(LedgerEntry).where(
            LedgerEntry.cycle_id == cycle_id,
            LedgerEntry.operation_type == "TRANSFER_IN",
        )).all()) == 1
        acks = session.exec(select(OutboundMessage).where(
            OutboundMessage.message_type == "ack",
            OutboundMessage.routing_key == "central",
        )).all()
        assert len(acks) == 6

    class LocalWallet:
        async def get_balance(self):
            return WalletBalanceResponse(
                city_id="KLD", kwh_available=Decimal("0"),
                kwh_held=Decimal("0"), cr_available=Decimal("500"),
                cr_held=Decimal("0"), credit_line=Decimal("0"),
                timestamp=datetime.now(timezone.utc).isoformat(),
                last_settled_cycle=None, balance_at_last_close_cr=None,
            )

        async def get_statement(self, cycle_id=None, cursor=None):
            return WalletStatementPage(entries=[], next_cursor=None)

    with Session(engine) as session:
        no_event = asyncio.run(reconcile_wallet(session, client=LocalWallet(), cycle_id=cycle_id))
        assert no_event.status == "NO_LOCAL_BASELINE"
        assert no_event.cr_available_local is None

    # El mismo método utilizado por los eventos Wallet crea el baseline EVENT.
    with Session(engine) as session:
        WalletService().record_balance_snapshot(
            session, cycle_id=cycle_id, source="EVENT",
            balances_dict={
                "CR": {"available": 500, "held": 0},
                "KWH": {"available": 0, "held": 0},
            },
        )
        session.commit()
        matched = asyncio.run(reconcile_wallet(session, client=LocalWallet(), cycle_id=cycle_id))
        assert matched.status == "DIVERGENT"
        assert matched.cr_available_local == Decimal("500")
        # La coincidencia EVENT/REST no oculta el desfase Wallet/ledger.
        assert session.get(Cycle, cycle_id).budget_balance == Decimal("120")
        assert matched.cr_available_wallet != session.get(Cycle, cycle_id).budget_balance
        assert Decimal(matched.explanation["local_evidence"]["single_cycle_cr"]["difference"]) == Decimal("380")

        opened = process_cycle_windows(session, now=now + timedelta(minutes=11))
        session.commit()
        assert opened["report_windows_opened"] == 1
        assert session.get(Cycle, cycle_id).scheduler_state == "REPORT_WINDOW"

        closed = process_cycle_windows(session, now=now + timedelta(minutes=16))
        session.commit()
        assert closed["cycles_closed"] == 1
        assert session.get(Cycle, cycle_id).scheduler_state == "CLOSED"
