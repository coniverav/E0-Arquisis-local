"""E2-035: la vista usa el snapshot transaccional E2-017, sin inventar saldo wallet."""
from datetime import datetime, timezone
import os
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlmodel import Session

os.environ.setdefault(
    "DATABASE_URL", "postgresql+psycopg://test:test@localhost/test"
)
from app.database import engine, run_migrations
from app.main import app
from app.models import Cycle


def setup_module():
    run_migrations()


def send(cycle, kind="offer", *, city="HGW", offer_id=None,
         timestamp="2026-10-25T12:00:00Z", **extra):
    payload = {
        "idpk": str(uuid4()), "msgId": str(uuid4()),
        "type": kind, "cycleId": cycle, "timestamp": timestamp,
    }
    if kind == "market-closed":
        payload.update(sender="central", data={})
    else:
        data = {"offerId": offer_id or f"{city}-{cycle}"}
        if kind == "offer":
            data.update(quantity=20, pricePerEnergy=330)
        elif kind == "offer-updated":
            data.update(remaining=12, pricePerEnergy=320)
        data.update(extra)
        payload.update(cityId=city, data=data)
    response = TestClient(app).post("/internal/audit/inbound/market", json=payload)
    assert response.status_code == 200, response.text
    return response


def book(cycle, **params):
    response = TestClient(app).get("/market/book", params={"cycleId": cycle, **params})
    assert response.status_code == 200, response.text
    return response.json()


def test_latest_offer_prices_sorted_per_cycle():
    cycle = f"cycle-{uuid4()}"
    other = f"cycle-{uuid4()}"
    send(cycle, city="HGW")
    send(cycle, city="COR", pricePerEnergy=315)
    send(other, city="HGW")
    result = book(cycle)
    assert result["total"] == 2
    assert [o["cityId"] for o in result["offers"]] == ["COR", "HGW"]
    send(cycle, "offer-updated", city="HGW",
         timestamp="2026-10-25T12:01:00Z", remaining=8, pricePerEnergy=300)
    result = book(cycle)
    assert [o["cityId"] for o in result["offers"]] == ["HGW", "COR"]
    assert result["offers"][0]["remaining"] == 8
    assert result["offers"][0]["lastEventAt"].startswith("2026-10-25T12:01:00")


def test_withdrawn_offer_hidden_but_auditable():
    cycle = f"cycle-{uuid4()}"
    send(cycle)
    send(cycle, "offer-withdrawn", timestamp="2026-10-25T12:02:00Z")
    assert book(cycle)["offers"] == []
    inactive = book(cycle, includeInactive="true")
    assert inactive["total"] == 1
    assert inactive["offers"][0]["state"] == "WITHDRAWN"
    assert inactive["offers"][0]["remaining"] == 0


def test_market_closed_is_terminal_for_book_and_preserves_history():
    cycle = f"cycle-{uuid4()}"
    send(cycle)
    send(cycle, "market-closed", timestamp="2026-10-25T12:05:00Z")
    assert book(cycle)["marketClosed"] is True
    assert book(cycle)["offers"] == []
    inactive = book(cycle, includeInactive="true")
    assert inactive["offers"][0]["state"] == "CLOSED"
    assert inactive["offers"][0]["stale"] is True


def test_out_of_order_offer_marked_uncertain_and_stale():
    cycle = f"cycle-{uuid4()}"
    send(cycle, "offer-updated", timestamp="2026-10-25T12:02:00Z")
    assert book(cycle)["total"] == 0
    send(cycle, timestamp="2026-10-25T12:00:00Z")
    result = book(cycle)
    assert result["total"] == 1
    assert result["offers"][0]["uncertain"] is True
    assert result["offers"][0]["stale"] is True
    assert result["offers"][0]["remaining"] == 12


def test_central_closes_at_hides_offer_without_market_closed_delivery():
    cycle = f"cycle-{uuid4()}"
    send(cycle)
    # Cycle es E1/v3; este placeholder representa un status-statement ya procesado.
    with Session(engine) as session:
        existing = session.get(Cycle, cycle)
        if existing is None:
            existing = Cycle(
                cycle_id=cycle, opening_budget_balance=0,
                opening_energy_balance=0, budget_balance=0, energy_balance=0,
                last_sequence=0, status_payload={},
                created_at=datetime.now(timezone.utc),
            )
            session.add(existing)
        existing.market_closes_at = datetime(2026, 10, 25, 12, 6, tzinfo=timezone.utc)
        session.commit()
    result = book(cycle)
    assert result["marketClosed"] is True
    assert result["offers"] == []
    assert result["closesAt"].startswith("2026-10-25T12:06:00")


def test_missing_cycle_has_empty_book_without_guessing_latest():
    unknown = book(f"cycle-{uuid4()}")
    assert unknown["total"] == 0
    assert unknown["marketClosed"] is False
