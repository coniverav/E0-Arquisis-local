"""E2-046: reentregas v3 integradas con PostgreSQL temporal y servicios externos simulados.

El connector y el backend procesan los mensajes reales. Cada test requiere una
base aislada cuyo nombre empiece con energyshark_test_ (ver conftest.py).
"""

import asyncio
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from sqlmodel import Session, select

from app.config import CITY_ID
from app.database import engine
from app.models import (
    InboundMessage, LedgerEntry, MarketBid, MarketEventClaim, MarketFeedOffer,
    MarketOffer, OfferReservation, OutboundMessage, ProcessedIdpk, SwapRecord,
    WalletBalanceSnapshot,
)
from app.services import wallet_client as wallet_client_module
from app.services.seller_offer import enqueue_seller_offer
from app.services.seller_swap_dispatch import dispatch_swap_proposals
from app.services.wallet_client import WalletClient
from tests.contracts_v3 import example, validate
from tests.test_e2045_seller_cycle import LocalBroker, _iso, clock, connector_module


def _rows(session, model, *conditions):
    return session.exec(select(model).where(*conditions)).all()


def _one(session, model, *conditions):
    return session.exec(select(model).where(*conditions)).one()


def _retry(payload, *, change_idpk=False, timestamp=None):
    repeated = deepcopy(payload)
    repeated["msgId"] = str(uuid4())
    if change_idpk:
        repeated["idpk"] = str(uuid4())
    if timestamp is not None:
        repeated["timestamp"] = _iso(timestamp)
    return repeated


def test_market_feed_same_idpk_new_msgid_keeps_one_snapshot_and_claim(
    clock, connector_module,
):
    broker = LocalBroker(connector_module)
    cycle_id = f"e2046-feed-{uuid4()}"
    offer = deepcopy(example("offer.json"))
    offer.update(idpk=str(uuid4()), msgId=str(uuid4()), cycleId=cycle_id,
                 cityId="MET", timestamp=_iso(clock.now))
    offer["data"]["offerId"] = f"MET-{cycle_id}"
    validate("amqp/offer", offer)

    async def scenario():
        await broker.deliver_market(offer, user_id="city.MET")
        with Session(engine) as session:
            snapshot = _one(session, MarketFeedOffer,
                            MarketFeedOffer.cycle_id == cycle_id)
            original = (snapshot.id, snapshot.remaining, snapshot.last_event_msg_id,
                        snapshot.uncertain)
            assert original[1:] == (offer["data"]["quantity"], offer["msgId"], False)

        retry = _retry(offer, timestamp=clock.now + timedelta(seconds=1))
        await broker.deliver_market(retry, user_id="city.MET")
        # Reutilizar la idpk con contenido económico distinto debe rechazarse.
        conflict = _retry(offer, timestamp=clock.now + timedelta(seconds=2))
        conflict["data"]["quantity"] += 500
        await broker.deliver_market(conflict, user_id="city.MET")
        with Session(engine) as session:
            snapshots = _rows(session, MarketFeedOffer,
                              MarketFeedOffer.cycle_id == cycle_id)
            claims = _rows(session, MarketEventClaim,
                           MarketEventClaim.sender == "MET",
                           MarketEventClaim.idpk == offer["idpk"])
            deliveries = _rows(session, InboundMessage,
                               InboundMessage.cycle_id == cycle_id,
                               InboundMessage.message_type == "offer")
            assert len(snapshots) == len(claims) == 1
            assert (snapshots[0].id, snapshots[0].remaining,
                    snapshots[0].last_event_msg_id, snapshots[0].uncertain) == original
            assert [row.status for row in deliveries] == [
                "PROCESSED", "DUPLICATE", "NOT_PROCESSED",
            ]
            assert [row.msg_id for row in deliveries] == [
                offer["msgId"], retry["msgId"], conflict["msgId"],
            ]
            assert not _rows(session, LedgerEntry, LedgerEntry.cycle_id == cycle_id)
            assert not _rows(session, SwapRecord, SwapRecord.cycle_id == cycle_id)
        print("E2-046 feed: 1 snapshot, 1 claim; replay=DUPLICATE; conflicto=NOT_PROCESSED")

    asyncio.run(scenario())


def test_seller_bid_and_wallet_replays_do_not_repeat_economic_effects(
    clock, connector_module, monkeypatch,
):
    broker = LocalBroker(connector_module)
    cycle_id = f"e2046-seller-{uuid4()}"
    offer_id = f"{CITY_ID}-{cycle_id}"
    buyer = "COR" if CITY_ID != "COR" else "HGW"
    swap_id = f"swp-{uuid4().hex}"
    key = ec.generate_private_key(ec.SECP256R1())
    wallet_calls = []

    def wallet_http(request):
        wallet_calls.append(request)
        assert request.url.host == "wallet.test"
        assert request.headers["X-Api-Key"] == "test-only"
        if request.method == "GET" and request.url.path == "/balance":
            balance = deepcopy(example("wallet-balance-response.json"))
            balance["cityId"] = CITY_ID
            balance["assets"]["KWH"]["available"] = 7300
            return httpx.Response(200, json=balance)
        assert request.method == "POST" and request.url.path == "/swaps"
        assert request.headers["Idempotency-Key"] == bid["idpk"]
        return httpx.Response(202, json={"accepted": True})

    observed = wallet_client_module.ObservedWalletAsyncClient
    monkeypatch.setattr(
        wallet_client_module, "ObservedWalletAsyncClient",
        lambda **kwargs: observed(transport=httpx.MockTransport(wallet_http), **kwargs),
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
    bid = {
        "idpk": str(uuid4()), "msgId": str(uuid4()), "type": "bid",
        "timestamp": _iso(clock.now), "cityId": buyer, "cycleId": cycle_id,
        "data": {"offerId": offer_id, "quantity": market["minBid"]},
    }

    async def scenario():
        await broker.deliver_city(status, user_id="central")
        assert await enqueue_seller_offer(cycle_id, wallet_client=wallet)
        with Session(engine) as session:
            offer = _one(session, MarketOffer, MarketOffer.offer_id == offer_id)
            initial_remaining = offer.remaining_quantity
            initial_ledger_count = len(_rows(session, LedgerEntry,
                                             LedgerEntry.cycle_id == cycle_id))

        await broker.deliver_city(bid, user_id=f"city.{buyer}")
        bid_retry = _retry(bid, timestamp=clock.now + timedelta(seconds=1))
        await broker.deliver_city(bid_retry, user_id=f"city.{buyer}")
        bid_conflict = _retry(bid, timestamp=clock.now + timedelta(seconds=2))
        bid_conflict["data"]["quantity"] += market["minBid"]
        await broker.deliver_city(bid_conflict, user_id=f"city.{buyer}")
        with Session(engine) as session:
            assert len(_rows(session, MarketBid, MarketBid.idpk == bid["idpk"])) == 1
            assert len(_rows(session, OfferReservation,
                             OfferReservation.bid_idpk == bid["idpk"])) == 1
            record = _one(session, SwapRecord, SwapRecord.idpk == bid["idpk"])
            assert record.status == "SUBMITTED"
            assert record.idempotency_key == bid["idpk"]
            assert len(_rows(session, ProcessedIdpk,
                             ProcessedIdpk.idpk == bid["idpk"])) == 1
            deliveries = _rows(session, InboundMessage,
                               InboundMessage.idpk == bid["idpk"],
                               InboundMessage.message_type == "bid")
            assert [row.status for row in deliveries] == [
                "PROCESSED", "DUPLICATE", "NOT_PROCESSED",
            ]
            amount = record.take_amount
            assert _one(session, MarketBid,
                        MarketBid.idpk == bid["idpk"]).quantity == market["minBid"]
            assert _one(session, OfferReservation,
                        OfferReservation.bid_idpk == bid["idpk"]).quantity == market["minBid"]

        result = await dispatch_swap_proposals(client=wallet, now=clock.now)
        assert result["accepted_202"] == 1
        assert (await dispatch_swap_proposals(client=wallet, now=clock.now))["claimed"] == 0
        assert [request.method for request in wallet_calls] == ["GET", "POST"]
        with Session(engine) as session:
            assert _one(session, SwapRecord, SwapRecord.idpk == bid["idpk"]).status == "SUBMITTED"
            assert _one(session, OfferReservation,
                        OfferReservation.bid_idpk == bid["idpk"]).status == "HOLD"
            assert not _rows(session, OutboundMessage,
                             OutboundMessage.cycle_id == cycle_id,
                             OutboundMessage.message_type == "bid-accepted")

        expires = clock.now + timedelta(seconds=45)
        give = {"asset": "KWH", "amount": market["minBid"]}
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
        await broker.deliver_city(_retry(proposed), user_id="central")
        with Session(engine) as session:
            assert _one(session, SwapRecord, SwapRecord.idpk == bid["idpk"]).status == "PROPOSED"
            assert len(_rows(session, OutboundMessage,
                             OutboundMessage.cycle_id == cycle_id,
                             OutboundMessage.message_type == "bid-accepted")) == 1
            assert {row.status for row in _rows(session, InboundMessage,
                                                InboundMessage.idpk == proposed["idpk"])} == {
                "PROCESSED", "DUPLICATE",
            }

        settled = deepcopy(example("swap-settled.json"))
        settled.update(idpk=str(uuid4()), msgId=str(uuid4()),
                       timestamp=_iso(clock.now))
        settled["data"].update(
            idpk=bid["idpk"], swapId=swap_id, proposer=CITY_ID,
            acceptor=buyer, give=give, take=take, memo=offer_id,
            settledAt=_iso(clock.now),
            balancesAfter={
                "KWH": {"available": 7300 - market["minBid"], "held": 0},
                "CR": {"available": float(amount), "held": 0},
            },
        )
        validate("amqp/swap-settled", settled)
        await broker.deliver_city(settled, user_id="central")
        with Session(engine) as session:
            committed = _one(session, MarketOffer, MarketOffer.offer_id == offer_id)
            assert committed.remaining_quantity == initial_remaining - market["minBid"]
            assert _one(session, OfferReservation,
                        OfferReservation.bid_idpk == bid["idpk"]).status == "COMMITTED"
            assert len(_rows(session, WalletBalanceSnapshot,
                             WalletBalanceSnapshot.cycle_id == cycle_id,
                             WalletBalanceSnapshot.source == "EVENT")) == 1
            assert len(_rows(session, OutboundMessage,
                             OutboundMessage.cycle_id == cycle_id,
                             OutboundMessage.message_type == "offer-updated")) == 1

        # Replay después del commit: mismo idpk con otro msgId y luego envelope
        # completamente nuevo para el mismo swapId (clave de negocio).
        await broker.deliver_city(_retry(settled), user_id="central")
        await broker.deliver_city(_retry(settled, change_idpk=True), user_id="central")
        await broker.deliver_city(_retry(bid), user_id=f"city.{buyer}")
        with Session(engine) as session:
            offer = _one(session, MarketOffer, MarketOffer.offer_id == offer_id)
            record = _one(session, SwapRecord, SwapRecord.idpk == bid["idpk"])
            assert record.status == "SETTLED" and record.swap_id == swap_id
            assert offer.remaining_quantity == initial_remaining - market["minBid"]
            assert len(_rows(session, MarketBid, MarketBid.idpk == bid["idpk"])) == 1
            assert len(_rows(session, OfferReservation,
                             OfferReservation.bid_idpk == bid["idpk"])) == 1
            assert len(_rows(session, SwapRecord, SwapRecord.idpk == bid["idpk"])) == 1
            assert len(_rows(session, WalletBalanceSnapshot,
                             WalletBalanceSnapshot.cycle_id == cycle_id,
                             WalletBalanceSnapshot.source == "EVENT")) == 1
            assert len(_rows(session, OutboundMessage,
                             OutboundMessage.cycle_id == cycle_id,
                             OutboundMessage.message_type == "bid-accepted")) == 1
            assert len(_rows(session, OutboundMessage,
                             OutboundMessage.cycle_id == cycle_id,
                             OutboundMessage.message_type == "offer-updated")) == 1
            # E2-051: un solo asiento por swapId, sin importar los replays.
            ledger = _rows(session, LedgerEntry,
                           LedgerEntry.cycle_id == cycle_id)
            assert len(ledger) == initial_ledger_count + 1
            assert ledger[0].operation_type == "SWAP_SETTLED"
            assert ledger[0].idpk == swap_id
            assert ledger[0].energy_delta == -market["minBid"]
            assert ledger[0].budget_delta == amount
            assert [row.status for row in _rows(session, InboundMessage,
                                                InboundMessage.idpk == settled["idpk"])] == [
                "PROCESSED", "DUPLICATE",
            ]
        assert [request.method for request in wallet_calls] == ["GET", "POST"]
        print("E2-046 seller: 1 bid, 1 reserva, 1 swap, 1 POST wallet, 1 liquidación;")
        print("replay con msgId nuevo y envelope nuevo: sin efecto económico adicional")

    asyncio.run(scenario())
