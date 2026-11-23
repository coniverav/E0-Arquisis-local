"""E3-043: settlement, remaining y offer-updated transaccionales.

Sólo PostgreSQL descartable energyshark_test_*. No requiere Wallet ni AWS.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Barrier
from uuid import uuid4

import pytest
from sqlmodel import Session, select

from app.config import CITY_ID
from app.database import engine, run_migrations
from app.models import (Cycle, LedgerEntry, MarketBid, MarketOffer,
                        OfferReservation, OutboundMessage, SwapRecord)
from app.services.seller_bid_acceptance import reserve_incoming_bid
from app.services.wallet_service import WalletService
from tests.test_e3041_seller_bid_accepted_db import seller_case
from tests.test_e3042_seller_terminal_db import _propose


def setup_module():
    assert (engine.url.database or "").startswith("energyshark_test_")
    run_migrations()


def _terminal(event, *, event_type="swap-settled", idpk=None):
    return {"swapId": event["swapId"], "idpk": idpk or event["idpk"]}


def _snapshot(case):
    _, cycle, offer_id, bid_idpk, _ = case
    with Session(engine) as session:
        offer = session.exec(select(MarketOffer).where(
            MarketOffer.offer_id == offer_id)).one()
        reservation = session.exec(select(OfferReservation).where(
            OfferReservation.bid_idpk == bid_idpk)).one()
        rows = session.exec(select(OutboundMessage).where(
            OutboundMessage.cycle_id == cycle,
            OutboundMessage.message_type == "offer-updated",
        ).order_by(OutboundMessage.id)).all()
        ledger = session.exec(select(LedgerEntry).where(
            LedgerEntry.cycle_id == cycle,
            LedgerEntry.operation_type == "SWAP_SETTLED",
        )).all()
        return offer.remaining_quantity, reservation.status, [r.payload for r in rows], len(ledger)


def _settle(event):
    with Session(engine) as session:
        record = WalletService(client=object()).process_swap_event(
            session, event_type="swap-settled", data=_terminal(event))
        assert record is not None
        session.commit()


def test_proposed_does_not_decrement_or_advertise(seller_case):
    event = _propose(seller_case)
    assert event["swapId"]
    assert _snapshot(seller_case) == (100, "HOLD", [], 0)


def test_settlement_commits_offer_and_outbox_in_one_transaction(seller_case):
    event = _propose(seller_case)
    with Session(engine) as session:
        record = WalletService(client=object()).process_swap_event(
            session, event_type="swap-settled", data=_terminal(event))
        assert record is not None
        cycle = seller_case[1]
        updated = session.exec(select(OutboundMessage).where(
            OutboundMessage.cycle_id == cycle,
            OutboundMessage.message_type == "offer-updated",
        )).one()
        offer = session.exec(select(MarketOffer).where(
            MarketOffer.offer_id == seller_case[2])).one()
        assert offer.remaining_quantity == 60
        assert updated.payload["data"] == {
            "offerId": seller_case[2], "remaining": 60, "pricePerEnergy": 4.25,
        }
        assert updated.exchange_name == "market"
        assert updated.routing_key == f"market.offer.{CITY_ID}"
        assert updated.status == "PENDING" and updated.dispatch_required is True
        assert updated.delivery_kind == "BROKER"
        session.commit()
    remaining, held, messages, ledger = _snapshot(seller_case)
    assert (remaining, held, ledger) == (60, "COMMITTED", 1)
    assert len(messages) == 1


def test_rollback_removes_offer_update_and_economic_effect(seller_case):
    event = _propose(seller_case)
    with Session(engine) as session:
        WalletService(client=object()).process_swap_event(
            session, event_type="swap-settled", data=_terminal(event))
        session.rollback()
    assert _snapshot(seller_case) == (100, "HOLD", [], 0)
    _settle(event)
    assert _snapshot(seller_case)[0:2] == (60, "COMMITTED")


def test_duplicate_settlements_reuse_ledger_and_outbox(seller_case):
    event = _propose(seller_case)
    _settle(event)
    for _ in range(3):
        with Session(engine) as session:
            rec = WalletService(client=object()).process_swap_event(
                session, event_type="swap-settled",
                data=_terminal(event, idpk=str(uuid4())))
            assert rec is not None
            session.commit()
    remaining, reservation, outbox, ledger = _snapshot(seller_case)
    assert (remaining, reservation, len(outbox), ledger) == (60, "COMMITTED", 1, 1)


@pytest.mark.parametrize("event_type", ["swap-rejected", "swap-expired", "swap-cancelled"])
def test_non_settlement_terminals_never_decrement(seller_case, event_type):
    event = _propose(seller_case)
    with Session(engine) as session:
        WalletService(client=object()).process_swap_event(
            session, event_type=event_type, data=_terminal(event))
        session.commit()
    assert _snapshot(seller_case) == (100, "RELEASED", [], 0)


def test_late_settlement_changes_remaining_without_late_offer_update(seller_case):
    event = _propose(seller_case)
    with Session(engine) as session:
        cycle = session.get(Cycle, seller_case[1])
        cycle.market_last_offer_at = seller_case[0] - timedelta(seconds=1)
        session.add(cycle)
        session.commit()
    _settle(event)
    assert _snapshot(seller_case) == (60, "COMMITTED", [], 1)


def test_two_sales_on_same_offer_use_committed_remaining(seller_case):
    now, cycle, offer_id, _, _ = seller_case
    first = _propose(seller_case)
    _settle(first)
    second_bid = str(uuid4())
    with Session(engine) as session:
        session.add(MarketBid(
            idpk=second_bid, msg_id=str(uuid4()), cycle_id=cycle,
            direction="INCOMING", offer_id=offer_id,
            counterpart_city_id="COR", quantity=25,
            status="PENDING", created_at=now,
        ))
        session.commit()
    with Session(engine) as session:
        assert reserve_incoming_bid(session, bid_idpk=second_bid, now=now)["status"] == "RESERVED"
        rec = session.exec(select(SwapRecord).where(SwapRecord.idpk == second_bid)).one()
        second_swap = "swp-" + uuid4().hex
        rec.swap_id = second_swap
        rec.status = "PROPOSED"
        session.add(rec)
        session.commit()
    with Session(engine) as session:
        WalletService(client=object()).process_swap_event(
            session, event_type="swap-settled",
            data={"idpk": second_bid, "swapId": second_swap})
        session.commit()
    remaining, _, updates, ledger = _snapshot(seller_case)
    assert (remaining, ledger) == (35, 2)
    assert [m["data"]["remaining"] for m in updates] == [60, 35]


def test_concurrent_delivery_only_one_offer_update(seller_case):
    event = _propose(seller_case)
    barrier = Barrier(2)

    def consume():
        with Session(engine) as session:
            barrier.wait(timeout=20)
            rec = WalletService(client=object()).process_swap_event(
                session, event_type="swap-settled",
                data=_terminal(event, idpk=str(uuid4())))
            assert rec is not None
            session.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(consume) for _ in range(2)]
        for future in futures:
            future.result(timeout=35)

    remaining, reservation, outbox, ledger = _snapshot(seller_case)
    assert (remaining, reservation, len(outbox), ledger) == (60, "COMMITTED", 1, 1)
