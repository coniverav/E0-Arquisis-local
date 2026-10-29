"""E2-063: selección por política versionada, con PostgreSQL y AD4 real."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import Barrier
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.config import CITY_ID
from app.database import engine
from app.main import app
from app.models import Cycle, MarketBid, OfferReservation, OutboundMessage, StrategyConfig, SwapRecord
from app.services.seller_bid_policy import (
    cycle_acceptance_policy, process_due_seller_bids, should_reserve_on_ingress,
)
from tests.test_seller_bid_acceptance import scenario, payload


def _strategy(s, policy):
    with Session(engine) as db:
        db.add(StrategyConfig(
            version=1, status="ACTIVE", created_by="e2063-test",
            created_at=s["now"] - timedelta(minutes=3),
            bid_acceptance_policy=policy,
            offer_selection_policy="CHEAPEST_EFFECTIVE_PRICE",
            offer_price_markup=Decimal("1.2000"),
            offer_quantity_fraction=Decimal("1.0000"), span_seconds=60,
            effective_from_cycle_id=s["cycle"],
        ))
        cycle = db.get(Cycle, s["cycle"])
        cycle.strategy_version = 1
        db.add(cycle)
        db.commit()


def _post(s, buyer, amount):
    msg = payload(s, buyer, amount)
    response = TestClient(app).post("/internal/messages", json=msg)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ok", response.text
    return msg


def _bids(s):
    with Session(engine) as db:
        return list(db.exec(select(MarketBid).where(
            MarketBid.cycle_id == s["cycle"], MarketBid.direction == "INCOMING"
        )).all())


def _holds(s):
    with Session(engine) as db:
        return list(db.exec(select(OfferReservation).where(
            OfferReservation.offer_id == s["offer"],
            OfferReservation.status == "HOLD"
        )).all())


def _swaps(s):
    with Session(engine) as db:
        return list(db.exec(select(SwapRecord).where(
            SwapRecord.cycle_id == s["cycle"],
            SwapRecord.role == "PROPOSER"
        )).all())


def _flush(s, *, seconds=4):
    with Session(engine) as db:
        result = process_due_seller_bids(db, now=datetime.now(timezone.utc) + timedelta(seconds=seconds))
        db.commit()
        return result


def test_fifo_acepta_al_ingresar_con_hold_y_swap(scenario):
    _strategy(scenario, "FIFO")
    with Session(engine) as db:
        assert should_reserve_on_ingress(db, scenario["cycle"]) is True
    msg = _post(scenario, "COR", 30)
    assert next(b for b in _bids(scenario) if b.idpk == msg["idpk"]).status == "RESERVED"
    assert len(_holds(scenario)) == 1
    assert len(_swaps(scenario)) == 1
    assert _flush(scenario)["reserved"] == 0


def test_max_remaining_prioriza_bid_mas_grande_sin_sobreventa(scenario):
    _strategy(scenario, "MAX_REMAINING")
    low = _post(scenario, "COR", 50)
    high = _post(scenario, "HGW", 70)
    assert len(_holds(scenario)) == 0
    assert len(_swaps(scenario)) == 0
    assert all(b.status == "PENDING" for b in _bids(scenario))
    out = _flush(scenario)
    assert out["reserved"] == 1 and out["rejected"] == 1
    status = {b.idpk: b for b in _bids(scenario)}
    assert status[high["idpk"]].status == "RESERVED"
    assert status[low["idpk"]].status == "REJECTED"
    assert status[low["idpk"]].available_on_rejection == 30
    assert sum(r.quantity for r in _holds(scenario)) == 70
    assert len(_swaps(scenario)) == 1
    with Session(engine) as db:
        rejected = db.exec(select(OutboundMessage).where(
            OutboundMessage.cycle_id == scenario["cycle"],
            OutboundMessage.message_type == "bid-rejected"
        )).one()
        assert rejected.routing_key == "city.COR"
        assert rejected.payload["reason"] == "QUANTITY_UNAVAILABLE"
        assert rejected.payload["data"]["available"] == 30


def test_batch_prioriza_cantidad_de_compradores(scenario):
    _strategy(scenario, "BATCH")
    large = _post(scenario, "COR", 70)
    a = _post(scenario, "HGW", 40)
    b = _post(scenario, "LIM", 40)
    assert all(row.status == "PENDING" for row in _bids(scenario))
    with Session(engine) as db:
        too_early = process_due_seller_bids(db, now=datetime.now(timezone.utc))
        db.commit()
        assert too_early["waiting"] == 3
    assert len(_holds(scenario)) == 0
    result = _flush(scenario)
    assert result["reserved"] == 2 and result["rejected"] == 1
    state = {r.idpk: r.status for r in _bids(scenario)}
    assert state[large["idpk"]] == "REJECTED"
    assert state[a["idpk"]] == state[b["idpk"]] == "RESERVED"
    assert sum(r.quantity for r in _holds(scenario)) == 80
    assert len(_swaps(scenario)) == 2


def test_cambio_pendiente_no_altera_politica_del_ciclo(scenario):
    _strategy(scenario, "FIFO")
    with Session(engine) as db:
        db.add(StrategyConfig(
            version=2, status="PENDING", created_by="operador",
            created_at=datetime.now(timezone.utc),
            bid_acceptance_policy="BATCH", offer_selection_policy="MIN_DISTANCE",
            offer_price_markup=Decimal("1.1"),
            offer_quantity_fraction=Decimal("0.9"), span_seconds=17,
        ))
        db.commit()
    with Session(engine) as db:
        assert cycle_acceptance_policy(db, scenario["cycle"]) == "FIFO"
    _post(scenario, "COR", 30)
    assert len(_holds(scenario)) == 1


def test_reintento_worker_no_acepta_ni_rechaza_dos_veces(scenario):
    _strategy(scenario, "MAX_REMAINING")
    _post(scenario, "COR", 80)
    _post(scenario, "HGW", 80)
    first = _flush(scenario)
    second = _flush(scenario)
    assert first["reserved"] == 1 and first["rejected"] == 1
    assert second["reserved"] == second["rejected"] == 0
    assert len(_holds(scenario)) == 1
    assert len(_swaps(scenario)) == 1


def test_dos_workers_comparten_lock_ad4(scenario):
    _strategy(scenario, "BATCH")
    _post(scenario, "COR", 75)
    _post(scenario, "HGW", 75)
    barrier = Barrier(2)
    when = datetime.now(timezone.utc) + timedelta(seconds=3)

    def run():
        barrier.wait(timeout=20)
        with Session(engine) as db:
            result = process_due_seller_bids(db, now=when)
            db.commit()
            return result

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(run)
        second = executor.submit(run)
        results = [first.result(timeout=30), second.result(timeout=30)]
    assert sum(r["reserved"] for r in results) == 1
    assert sum(r["rejected"] for r in results) == 1
    assert len(_holds(scenario)) == 1
    assert len(_swaps(scenario)) == 1


def test_no_se_reserva_despues_de_last_bid_at(scenario):
    _strategy(scenario, "BATCH")
    _post(scenario, "COR", 30)
    with Session(engine) as db:
        cycle = db.get(Cycle, scenario["cycle"])
        overdue = cycle.market_last_bid_at + timedelta(seconds=1)
        result = process_due_seller_bids(db, now=overdue)
        db.commit()
    assert result["reserved"] == 0
    assert result["rejected"] == 1
    assert _bids(scenario)[0].rejection_reason == "MARKET_CLOSED"
    assert len(_holds(scenario)) == 0
