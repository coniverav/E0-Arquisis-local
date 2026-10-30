"""E2-045: ensayo buyer v3 local; MET, broker y wallet son simulados.

El bid se dispara por el endpoint interno existente. E2-051 aplica swap,
transporte y settlement al ledger E1; el cierre deja KWH local en 0.
Ejecutar solo contra una PostgreSQL temporal energyshark_test_*.
"""

import asyncio
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
import json
from uuid import uuid4

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from jwt.algorithms import ECAlgorithm
from sqlmodel import Session, select

from app.config import CITY_ID
from app.database import engine
from app.main import app
from app.models import (
    Cycle, DistanceTable, ImbalanceSettlement, InboundMessage, LedgerEntry, MarketBid,
    MarketFeedOffer, NegotiationReport, OutboundMessage, SwapRecord, TradeRecord,
    WalletBalanceSnapshot,
)
from app.services import swap_tokens, wallet_client as wallet_client_module
from app.services.swap_accept import accept_pending_swaps
from app.services.cycle_scheduler import process_cycle_windows
from app.services.negotiation_report_dispatch import enqueue_due_negotiation_reports
from app.services.wallet_client import WalletClient
from app.services.wallet_service import WalletService
from tests.contracts_v3 import example, validate
from tests.test_e2045_seller_cycle import LocalBroker, _iso, clock, connector_module


def _one(session, model, *conditions):
    return session.exec(select(model).where(*conditions)).one()


def test_buyer_met_offer_to_settlement_with_visible_ledger_gap(
    clock, connector_module, monkeypatch,
):
    assert (engine.url.database or "").startswith("energyshark_test_")
    broker = LocalBroker(connector_module)
    cycle_id = f"e2045-buyer-{uuid4()}"
    offer_id = f"MET-{cycle_id}"
    swap_id = f"swp-{uuid4().hex}"
    quantity = 1000  # Cubre exactamente el déficit sintético del ciclo.
    price = Decimal("661.50")  # 90 % de imbalancePrice=735: oferta inicial de MET.
    amount = price * quantity
    transport_total = Decimal("1000.00")
    starting_cr = Decimal("1000000.00")
    after_swap_cr = starting_cr - amount
    after_transport_cr = after_swap_cr - transport_total

    private_key = ec.generate_private_key(ec.SECP256R1())
    public_jwk = json.loads(ECAlgorithm.to_jwk(private_key.public_key()))
    public_jwk.update(kid="wallet-test-kid", use="sig", alg="ES256")
    swap_tokens.clear_jwks_cache()
    monkeypatch.setattr(
        swap_tokens, "_fetch_jwks", lambda *_args, **_kwargs: {"keys": [public_jwk]},
    )
    wallet_calls = []

    def wallet_http(request):
        wallet_calls.append(request)
        assert request.url.host == "wallet.test"
        assert request.method == "POST"
        assert request.url.path == f"/swaps/{swap_id}/accept"
        assert request.headers["X-Api-Key"] == "test-only"
        assert request.headers["Idempotency-Key"]
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
        generationCapacity=9000, consumption=10000, sellable=0,
    )
    market = status["data"]["market"]
    market.update(
        opensAt=_iso(clock.now - timedelta(minutes=2)),
        lastOfferAt=_iso(clock.now + timedelta(minutes=4, seconds=30)),
        lastBidAt=_iso(clock.now + timedelta(minutes=5)),
        closesAt=_iso(clock.now + timedelta(minutes=6)),
        reportOpensAt=_iso(clock.now + timedelta(minutes=6)),
    )
    status["data"]["validUntil"] = _iso(clock.now + timedelta(minutes=7))
    validate("amqp/status-statement", status)

    distance = {
        "idpk": str(uuid4()), "msgId": str(uuid4()),
        "type": "distance-table", "sender": "central",
        "timestamp": _iso(clock.now), "cycleId": cycle_id,
        "data": {
            "distances": {"MET": {
                "distance": 1000, "transportCost": 1, "enabled": True,
            }},
            "disabledRouteTransportCost": 10,
        },
    }
    offer = {
        "idpk": str(uuid4()), "msgId": str(uuid4()),
        "type": "offer", "cityId": "MET", "cycleId": cycle_id,
        # El broker simulado abre el consumidor en esta entrega. E2-072 marca
        # como inciertas las ofertas anteriores a esa conexión; MET publica
        # aquí una oferta nueva después de la señal de recuperación.
        "timestamp": _iso(clock.now + timedelta(seconds=15)),
        "data": {"offerId": offer_id, "quantity": 1200,
                 "pricePerEnergy": float(price)},
    }
    validate("amqp/offer", offer)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as backend:
            await broker.deliver_city(status, user_id="central")
            await broker.deliver_city(distance, user_id="central")
            await broker.deliver_market(offer, user_id="city.MET")
            with Session(engine) as session:
                cycle = session.get(Cycle, cycle_id)
                assert cycle.status_idpk == status["idpk"]
                assert cycle.energy_balance == Decimal("-1000")
                assert _one(session, DistanceTable,
                            DistanceTable.cycle_id == cycle_id).distances["MET"]["enabled"]
                assert _one(session, MarketFeedOffer,
                            MarketFeedOffer.offer_id == offer_id).remaining == 1200
                initial_ledger = session.exec(select(LedgerEntry).where(
                    LedgerEntry.cycle_id == cycle_id)).all()
                initial_budget = cycle.budget_balance
                initial_energy = cycle.energy_balance

            candidates = await backend.get("http://master/market/buy-candidates",
                                           params={"cycleId": cycle_id})
            assert candidates.status_code == 200, candidates.text
            assert candidates.json()["canBid"] is True
            assert candidates.json()["candidates"][0]["offerId"] == offer_id
            assert Decimal(str(candidates.json()["candidates"][0][
                "effectivePricePerEnergy"])) == price + Decimal("1.00")

            # Este POST sustituye solo el disparador autónomo aún inexistente.
            bid_idpk = str(uuid4())
            created = await backend.post("http://master/internal/market/bids", json={
                "cycleId": cycle_id, "offerId": offer_id,
                "quantity": quantity, "idpk": bid_idpk,
            })
            assert created.status_code == 201, created.text
            bid = created.json()
            repeated = await backend.post("http://master/internal/market/bids", json={
                "cycleId": cycle_id, "offerId": offer_id,
                "quantity": quantity, "idpk": bid_idpk,
            })
            assert repeated.status_code == 201 and repeated.json()["duplicate"]
            with Session(engine) as session:
                local_bid = _one(session, MarketBid, MarketBid.idpk == bid_idpk)
                assert local_bid.status == "PENDING"
                assert local_bid.counterpart_city_id == "MET"
                assert local_bid.quantity == quantity
                outbound = _one(session, OutboundMessage,
                                OutboundMessage.msg_id == bid["msgId"])
                assert outbound.idpk == bid_idpk
                assert outbound.payload["data"] == {
                    "offerId": offer_id, "quantity": quantity,
                }
            published = await broker.publish_outbox(backend, cycle_id=cycle_id)
            assert [item["type"] for item in published] == ["bid"]
            assert broker.directed.published[-1][0] == "city.MET"
            assert broker.directed.published[-1][1].user_id == f"city.{CITY_ID}"

            expires = clock.now + timedelta(seconds=45)
            give = {"asset": "KWH", "amount": quantity}
            take = {"asset": "CR", "amount": float(amount)}
            token = jwt.encode({
                "iss": "wallet.energyshark", "sub": swap_id,
                "iat": int(clock.now.timestamp()), "exp": int(expires.timestamp()),
                "proposer": "MET", "give": give, "take": take, "memo": offer_id,
            }, private_key, algorithm="ES256", headers={"kid": "wallet-test-kid"})
            accepted = {
                "idpk": str(uuid4()), "msgId": str(uuid4()),
                "type": "bid-accepted", "cityId": "MET", "cycleId": cycle_id,
                "timestamp": _iso(clock.now),
                "data": {
                    "target": bid["msgId"], "offerId": offer_id,
                    "quantity": quantity, "pricePerEnergy": float(price),
                    "amount": float(amount), "swapId": swap_id,
                    "swapToken": token, "expiresAt": _iso(expires),
                },
            }
            await broker.deliver_city(accepted, user_id="city.MET")
            # E2-046: el vendedor reenvía la misma aceptación con otro msgId.
            accepted_retry = deepcopy(accepted)
            accepted_retry["msgId"] = str(uuid4())
            await broker.deliver_city(accepted_retry, user_id="city.MET")
            with Session(engine) as session:
                assert _one(session, MarketBid,
                            MarketBid.idpk == bid_idpk).status == "ACCEPTED"
                assert session.exec(select(SwapRecord).where(
                    SwapRecord.cycle_id == cycle_id)).all() == []
                replies = session.exec(select(InboundMessage).where(
                    InboundMessage.idpk == accepted["idpk"],
                    InboundMessage.message_type == "bid-accepted",
                )).all()
                assert [row.status for row in replies] == ["PROCESSED", "DUPLICATE"]

            with Session(engine) as session:
                result = await accept_pending_swaps(
                    session, now=clock.now,
                    wallet_service=WalletService(client=wallet),
                )
                assert result["accepted"] == 1
                record = _one(session, SwapRecord,
                              SwapRecord.source_bid_idpk == bid_idpk)
                assert record.role == "ACCEPTOR"
                assert record.swap_id == swap_id
                assert record.give_asset == "CR" and record.give_amount == amount
                assert record.take_asset == "KWH" and record.take_amount == quantity
                assert record.status == "ACCEPT_SUBMITTED"
                assert record.accept_idempotency_key == record.idpk
                accept_idpk = record.idpk
                again = await accept_pending_swaps(
                    session, now=clock.now,
                    wallet_service=WalletService(client=wallet),
                )
                assert again["accepted"] == 0
            assert len(wallet_calls) == 1  # HTTP 202 no equivale a swap-settled.
            assert wallet_calls[0].headers["Idempotency-Key"] == accept_idpk

            settled = deepcopy(example("swap-settled.json"))
            settled.update(idpk=str(uuid4()), msgId=str(uuid4()),
                           timestamp=_iso(clock.now))
            settled["data"].update(
                swapId=swap_id, idpk=accept_idpk,
                proposer="MET", acceptor=CITY_ID,
                give=give, take=take, memo=offer_id,
                settledAt=_iso(clock.now),
                balancesAfter={
                    "CR": {"available": float(after_swap_cr), "held": 0},
                    "KWH": {"available": quantity, "held": 0},
                },
            )
            validate("amqp/swap-settled", settled)
            await broker.deliver_city(settled, user_id="central")
            settled_retry = deepcopy(settled)
            settled_retry["msgId"] = str(uuid4())
            await broker.deliver_city(settled_retry, user_id="central")
            new_envelope = deepcopy(settled)
            new_envelope["idpk"] = str(uuid4())
            new_envelope["msgId"] = str(uuid4())
            await broker.deliver_city(new_envelope, user_id="central")
            with Session(engine) as session:
                record = _one(session, SwapRecord,
                              SwapRecord.source_bid_idpk == bid_idpk)
                assert record.status == "SETTLED"
                assert len(session.exec(select(SwapRecord).where(
                    SwapRecord.source_bid_idpk == bid_idpk)).all()) == 1
                snapshot = _one(session, WalletBalanceSnapshot,
                                WalletBalanceSnapshot.cycle_id == cycle_id,
                                WalletBalanceSnapshot.source == "EVENT")
                assert snapshot.kwh_available == quantity
                assert snapshot.cr_available == after_swap_cr
                assert len(session.exec(select(WalletBalanceSnapshot).where(
                    WalletBalanceSnapshot.cycle_id == cycle_id,
                    WalletBalanceSnapshot.source == "EVENT",
                )).all()) == 1
                settlements = session.exec(select(InboundMessage).where(
                    InboundMessage.idpk == settled["idpk"],
                    InboundMessage.message_type == "swap-settled",
                )).all()
                assert {row.msg_id: row.status for row in settlements} == {
                    settled["msgId"]: "PROCESSED",
                    settled_retry["msgId"]: "DUPLICATE",
                }

            trade = deepcopy(example("trade-recorded.json"))
            trade.update(idpk=str(uuid4()), msgId=str(uuid4()),
                         timestamp=_iso(clock.now), cycleId=cycle_id)
            trade["data"].update(
                swapId=swap_id, offerId=offer_id,
                seller="MET", buyer=CITY_ID, quantity=quantity,
                pricePerEnergy=float(price), energyTotal=float(amount),
                transportPerEnergy=1, transportTotal=float(transport_total),
                routeEnabled=True,
            )
            validate("amqp/trade-recorded", trade)
            await broker.deliver_city(trade, user_id="central")
            trade_retry = deepcopy(trade)
            trade_retry["msgId"] = str(uuid4())
            await broker.deliver_city(trade_retry, user_id="central")
            trade_new_envelope = deepcopy(trade)
            trade_new_envelope.update(idpk=str(uuid4()), msgId=str(uuid4()))
            await broker.deliver_city(trade_new_envelope, user_id="central")
            with Session(engine) as session:
                recorded = _one(session, TradeRecord, TradeRecord.swap_id == swap_id)
                assert recorded.role == "BUYER"
                assert recorded.transport_total == transport_total
                assert len(session.exec(select(TradeRecord).where(
                    TradeRecord.swap_id == swap_id)).all()) == 1

            # Cierre y liquidación sintéticos: se verifica persistencia, no
            # corrección económica del reporte ni aprobación por la central.
            clock.now += timedelta(minutes=6)
            close = {
                "idpk": str(uuid4()), "msgId": str(uuid4()),
                "type": "market-closed", "sender": "central",
                "timestamp": _iso(clock.now), "cycleId": cycle_id, "data": {},
            }
            await broker.deliver_market(close, user_id=None)
            imbalance = deepcopy(example("imbalance-settlement.json"))
            imbalance.update(idpk=str(uuid4()), msgId=str(uuid4()),
                             timestamp=_iso(clock.now), cycleId=cycle_id)
            imbalance["data"].update(
                kwhHeld=quantity, deficit=quantity, coverage=0,
                pricePerEnergy=market["buybackPrice"], total=0,
                balanceAfter=float(after_transport_cr), settlementId=str(uuid4()),
            )
            validate("amqp/imbalance-settlement", imbalance)
            await broker.deliver_city(imbalance, user_id="central")
            imbalance_retry = deepcopy(imbalance)
            imbalance_retry["msgId"] = str(uuid4())
            await broker.deliver_city(imbalance_retry, user_id="central")
            imbalance_new_envelope = deepcopy(imbalance)
            imbalance_new_envelope.update(idpk=str(uuid4()), msgId=str(uuid4()))
            await broker.deliver_city(imbalance_new_envelope, user_id="central")
            with Session(engine) as session:
                closed = _one(session, ImbalanceSettlement,
                              ImbalanceSettlement.cycle_id == cycle_id)
                assert closed.coverage == 0 and closed.balance_after == after_transport_cr
                assert len(session.exec(select(ImbalanceSettlement).where(
                    ImbalanceSettlement.cycle_id == cycle_id)).all()) == 1
                feed = _one(session, MarketFeedOffer,
                            MarketFeedOffer.offer_id == offer_id)
                assert feed.state == "CLOSED"
                cycle = session.get(Cycle, cycle_id)
                final_ledger = session.exec(select(LedgerEntry).where(
                    LedgerEntry.cycle_id == cycle_id)).all()
                # E2-051: swap, transporte y settlement sí actualizan el E1.
                assert len(final_ledger) == len(initial_ledger) + 3
                assert {entry.operation_type for entry in final_ledger} == {
                    "SWAP_SETTLED", "SWAP_TRANSPORT", "IMBALANCE_SETTLEMENT",
                }
                assert cycle.budget_balance == -amount - transport_total
                assert cycle.energy_balance == Decimal("0")
                assert cycle.budget_balance != closed.balance_after

            with Session(engine) as session:
                windows = process_cycle_windows(session, now=clock.now)
                assert windows["report_windows_opened"] == 1
                assert enqueue_due_negotiation_reports(
                    session, city_id=CITY_ID, routing_key="central", now=clock.now,
                ) == 1
                session.commit()
                report = _one(session, NegotiationReport,
                              NegotiationReport.cycle_id == cycle_id)
                # E2-052: el reporte sale después del cierre y refleja el
                # ledger final (no el saldo wallet de la central).
                assert report.payload["data"] == {
                    "budgetBalance": float(-amount - transport_total),
                    "energyBalance": 0.0,
                }
                assert Decimal(str(report.payload["data"]["budgetBalance"])) != after_transport_cr
            published = await broker.publish_outbox(backend, cycle_id=cycle_id)
            assert [item["type"] for item in published] == ["negotiation-report"]
            assert broker.directed.published[-1][0] == "central"
            print(
                "E2-045 buyer local: offer_MET=recibida bid=PUBLISHED "
                "wallet_202=ACCEPT_SUBMITTED swap=SETTLED trade=BUYER "
                "imbalance=0 report=PUBLISHED_en_broker_simulado "
                f"wallet_CR_cierre={after_transport_cr} "
                f"ledger_CR={initial_budget} ledger_KWH={initial_energy}"
            )

    try:
        asyncio.run(scenario())
    finally:
        swap_tokens.clear_jwks_cache()
