"""E2-043: `trade-recorded` e `imbalance-settlement` por el ingress real.

Se usan los ejemplos oficiales vendorizados. La deduplicación se prueba con
envelopes nuevos (otro `idpk`/`msgId`) para confirmar que la identidad es el
`swapId` o el `settlementId`, no el mensaje.
"""

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlmodel import Session, select

from app.database import engine, run_migrations
from app.main import app
from app.models import ImbalanceSettlement, TradeRecord
from tests.contracts_v3 import example, validate


def setup_module():
    run_migrations()


@pytest.fixture()
def client():
    return TestClient(app)


def _envelope(name: str, *, cycle_id: str | None = None):
    payload = deepcopy(example(name))
    payload["idpk"] = str(uuid4())
    payload["msgId"] = str(uuid4())
    payload["timestamp"] = datetime.now(timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )
    payload["cycleId"] = cycle_id or f"cycle-{uuid4().hex[:8]}"
    return payload


def _post(client, payload):
    response = client.post("/internal/messages", json=payload)
    assert response.status_code == 200, response.text
    return response


def test_trade_recorded_as_seller_persists_transport(client):
    payload = _envelope("trade-recorded.json")
    validate("amqp/trade-recorded", payload)

    assert _post(client, payload).json()["status"] == "ok"

    with Session(engine) as session:
        record = session.exec(select(TradeRecord)).one()
    assert record.swap_id == payload["data"]["swapId"]
    assert record.seller == "KLD" and record.buyer == "COR"
    assert record.role == "SELLER"
    assert record.counterpart_city_id == "COR"
    assert record.quantity == payload["data"]["quantity"]
    assert record.transport_per_energy == Decimal(
        str(payload["data"]["transportPerEnergy"])
    )
    assert record.transport_total == Decimal(
        str(payload["data"]["transportTotal"])
    )
    assert record.route_enabled is True


def test_trade_recorded_as_buyer_uses_buyer_perspective(client):
    payload = _envelope("trade-recorded.json")
    payload["data"]["seller"] = "COR"
    payload["data"]["buyer"] = "KLD"
    validate("amqp/trade-recorded", payload)

    _post(client, payload)

    with Session(engine) as session:
        record = session.exec(select(TradeRecord)).one()
    assert record.role == "BUYER"
    assert record.counterpart_city_id == "COR"


def test_trade_recorded_duplicate_with_new_envelope_is_deduplicated(client):
    first = _envelope("trade-recorded.json")
    _post(client, first)

    replay = deepcopy(first)
    replay["idpk"] = str(uuid4())
    replay["msgId"] = str(uuid4())
    replay["timestamp"] = datetime.now(timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )
    response = _post(client, replay)

    assert response.json()["status"] == "duplicate"
    with Session(engine) as session:
        records = session.exec(select(TradeRecord)).all()
    assert len(records) == 1


def test_trade_recorded_conflict_is_not_processed(client):
    first = _envelope("trade-recorded.json")
    _post(client, first)

    conflict = deepcopy(first)
    conflict["idpk"] = str(uuid4())
    conflict["msgId"] = str(uuid4())
    conflict["data"]["transportTotal"] += 1
    response = _post(client, conflict)

    assert response.json()["status"] == "not_processed"
    assert response.json()["reason"] == "TRADE_CONFLICT"
    with Session(engine) as session:
        records = session.exec(select(TradeRecord)).all()
    assert len(records) == 1


def test_trade_recorded_before_swap_settled_is_persisted(client):
    payload = _envelope("trade-recorded.json")

    assert _post(client, payload).json()["status"] == "ok"

    with Session(engine) as session:
        record = session.exec(select(TradeRecord)).one()
    assert record.cycle_id == payload["cycleId"]


def test_imbalance_settlement_is_persisted(client):
    payload = _envelope("imbalance-settlement.json")
    validate("amqp/imbalance-settlement", payload)

    assert _post(client, payload).json()["status"] == "ok"

    with Session(engine) as session:
        record = session.exec(select(ImbalanceSettlement)).one()
    assert record.settlement_id == payload["data"]["settlementId"]
    assert record.coverage == payload["data"]["coverage"]
    assert record.total == Decimal(str(payload["data"]["total"]))
    assert record.balance_after == Decimal(str(payload["data"]["balanceAfter"]))
    assert record.kwh_held == payload["data"]["kwhHeld"]
    assert record.deficit == payload["data"]["deficit"]


def test_imbalance_duplicate_with_new_envelope_is_deduplicated(client):
    first = _envelope("imbalance-settlement.json")
    _post(client, first)

    replay = deepcopy(first)
    replay["idpk"] = str(uuid4())
    replay["msgId"] = str(uuid4())
    response = _post(client, replay)

    assert response.json()["status"] == "duplicate"
    with Session(engine) as session:
        records = session.exec(select(ImbalanceSettlement)).all()
    assert len(records) == 1


def test_imbalance_inconsistent_total_is_not_processed(client):
    payload = _envelope("imbalance-settlement.json")
    payload["data"]["total"] += 1

    response = _post(client, payload)

    assert response.json()["status"] == "not_processed"
    assert response.json()["reason"] == "IMBALANCE_TOTAL_MISMATCH"
    with Session(engine) as session:
        assert session.exec(select(ImbalanceSettlement)).all() == []


def test_imbalance_coverage_mismatch_is_not_processed(client):
    payload = _envelope("imbalance-settlement.json")
    payload["data"]["coverage"] += 1

    response = _post(client, payload)

    assert response.json()["status"] == "not_processed"
    assert response.json()["reason"] == "IMBALANCE_COVERAGE_MISMATCH"
