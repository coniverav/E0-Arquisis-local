"""E2-050: funding v3 estricto y auditoría de wallet."""

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.database import engine
from app.main import app
from app.models import Cycle, LedgerEntry


def _payload(cycle_id: str, **overrides):
    data = {
        "quantity": 1000,
        "balanceAfter": 1000,
        "creditLine": 500,
    }
    data.update(overrides)
    return {
        "idpk": str(uuid4()),
        "msgId": str(uuid4()),
        "type": "transfer",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "cycleId": cycle_id,
        "sender": "central",
        "data": data,
    }


def test_funding_v3_audits_penalty_without_double_debit():
    client = TestClient(app)
    cycle_id = f"e2050-{uuid4()}"
    payload = _payload(
        cycle_id,
        quantity=900,
        balanceAfter=850,
        creditLine=500,
        penalty={"amount": 100, "forCycleId": "previous"},
    )

    response = client.post("/internal/messages", json=payload)

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    with Session(engine) as session:
        cycle = session.get(Cycle, cycle_id)
        assert cycle.budget_balance == Decimal("900.00")
        entries = session.exec(
            select(LedgerEntry).where(LedgerEntry.cycle_id == cycle_id)
        ).all()
        assert len(entries) == 1
        entry = entries[0]
        assert entry.operation_type == "TRANSFER_IN"
        assert entry.budget_delta == Decimal("900.00")
        funding = entry.details["funding"]
        assert funding["quantity"] == "900"
        assert funding["balanceAfter"] == "850"
        assert funding["creditLine"] == "500"
        assert funding["penalty"] == {"amount": 100, "forCycleId": "previous"}
        assert Decimal(funding["balanceDifference"]) == Decimal("50.00")


def test_funding_v3_without_balance_after_is_not_processed():
    client = TestClient(app)
    cycle_id = f"e2050-{uuid4()}"
    payload = _payload(cycle_id)
    payload["data"].pop("balanceAfter")

    response = client.post("/internal/messages", json=payload)

    assert response.status_code == 200
    assert response.json()["status"] == "not_processed"
    assert response.json()["reason"] == "INVALID_FUNDING_V3"
    with Session(engine) as session:
        cycle = session.get(Cycle, cycle_id)
        assert cycle is not None
        assert cycle.budget_balance == Decimal("0.00")
        assert session.exec(
            select(LedgerEntry).where(LedgerEntry.cycle_id == cycle_id)
        ).all() == []
