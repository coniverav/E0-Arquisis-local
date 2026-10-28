"""E2-057: interfaz de lectura para E2-058/060 y edición AD6 protegida.

Ejecutar solo con DATABASE_URL en energyshark_test_* (ver conftest.py).
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.auth import verify_jwt
from app.database import engine
from app.main import app
from app.models import Cycle, MarketOffer, StrategyConfig, SwapRecord
from app.services.strategy_config import bind_strategy_to_cycle

client = TestClient(app)


POLICY = {
    "offerPriceMarkup": "1.17",
    "offerQuantityFraction": "0.8",
    "bidAcceptancePolicy": "FIFO",
    "offerSelectionPolicy": "MIN_DISTANCE",
    "spanSeconds": 45,
}


def _authorized_post(payload):
    app.dependency_overrides[verify_jwt] = lambda: {"sub": "operator-test"}
    try:
        return client.post("/market/strategy", json=payload)
    finally:
        app.dependency_overrides.pop(verify_jwt, None)


def test_read_strategy_is_empty_and_side_effect_free():
    result = client.get("/market/strategy")
    assert result.status_code == 200
    assert result.json() == {"active": None, "pending": None, "cycleStrategy": None}
    assert client.get("/market/strategies").json() == []
    with Session(engine) as session:
        assert session.exec(select(StrategyConfig)).all() == []


def test_unknown_cycle_strategy_gives_404():
    result = client.get("/market/strategy", params={"cycleId": "missing"})
    assert result.status_code == 404


def test_write_strategy_requires_jwt_and_rejects_invalid_policy():
    assert client.post("/market/strategy", json=POLICY).status_code == 401
    invalid = _authorized_post({**POLICY, "bidAcceptancePolicy": "UNKNOWN"})
    assert invalid.status_code == 422
    assert _authorized_post({**POLICY, "spanSeconds": -1}).status_code == 422
    assert _authorized_post({**POLICY, "offerQuantityFraction": "1.5"}).status_code == 422
    assert _authorized_post({**POLICY, "unexpected": "x"}).status_code == 422
    with Session(engine) as session:
        assert session.exec(select(StrategyConfig)).all() == []


def test_authenticated_write_creates_pending_version_and_leaves_initial_active():
    response = _authorized_post(POLICY)
    assert response.status_code == 201, response.text
    assert response.json()["version"] == 2
    assert response.json()["status"] == "PENDING"
    assert response.json()["createdBy"] == "operator-test"
    assert response.json()["spanSeconds"] == 45
    read = client.get("/market/strategy").json()
    assert read["active"]["version"] == 1
    assert read["pending"]["version"] == 2
    assert [item["version"] for item in client.get("/market/strategies").json()] == [2, 1]


def test_midcycle_update_preserves_strategy_bound_to_cycle():
    now = datetime.now(timezone.utc)
    cycle_id = f"e2057-{uuid4()}"
    with Session(engine) as session:
        cycle = Cycle(cycle_id=cycle_id, created_at=now,
                      market_opens_at=now - timedelta(seconds=60))
        session.add(cycle)
        session.flush()
        baseline = bind_strategy_to_cycle(session, cycle)
        assert baseline.version == 1
        session.commit()
    changed = _authorized_post(POLICY)
    assert changed.status_code == 201
    status = client.get("/market/strategy", params={"cycleId": cycle_id})
    assert status.status_code == 200
    data = status.json()
    assert data["cycleStrategy"]["version"] == 1
    assert data["cycleStrategy"]["spanSeconds"] == 60
    assert data["pending"]["version"] == 2


def _record(session, *, cycle_id, status, role, now, swap_id=None):
    swap = SwapRecord(
        idpk=str(uuid4()), swap_id=swap_id or f"s-{uuid4()}",
        cycle_id=cycle_id, role=role, counterpart_city_id="MET",
        give_asset="CR", give_amount=Decimal("200"),
        take_asset="KWH", take_amount=Decimal("100"),
        memo="offer-id", status=status,
        idempotency_key=f"private-key-{uuid4()}",
        swap_token="secret-jwt-NEVER-PUBLISH",
        source_bid_idpk=None,
        created_at=now, expires_at=now + timedelta(minutes=3),
        proposal_http_accepted_at=now if role == "PROPOSER" else None,
    )
    session.add(swap)
    session.flush()
    return swap.id


def test_swap_listing_filters_pagination_and_does_not_expose_secrets():
    now = datetime.now(timezone.utc)
    cycle_a = f"e2057-{uuid4()}"
    cycle_b = f"e2057-{uuid4()}"
    with Session(engine) as session:
        first = _record(session, cycle_id=cycle_a, status="PROPOSED",
                        role="PROPOSER", now=now - timedelta(seconds=1))
        second = _record(session, cycle_id=cycle_a, status="SETTLED",
                         role="ACCEPTOR", now=now)
        _record(session, cycle_id=cycle_b, status="PROPOSED", role="ACCEPTOR", now=now)
        session.commit()
    result = client.get("/market/swaps", params={"cycleId": cycle_a, "limit": 1})
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["total"] == 2
    assert len(data["items"]) == 1
    assert data["items"][0]["id"] == second
    assert "secret-jwt" not in result.text
    assert "idempotencyKey" not in result.text
    older = client.get("/market/swaps", params={"cycleId": cycle_a, "offset": 1, "limit": 1}).json()
    assert older["items"][0]["id"] == first
    status = client.get("/market/swaps", params={"status": "SETTLED", "role": "ACCEPTOR"}).json()
    assert status["total"] == 1
    assert status["items"][0]["status"] == "SETTLED"
    assert client.get("/market/swaps", params={"limit": 300}).status_code == 422


def test_swap_detail_is_404_for_unknown_and_never_exposes_token():
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        record_id = _record(session, cycle_id=f"e2057-{uuid4()}",
                            status="SUBMITTED", role="PROPOSER", now=now)
        session.commit()
    result = client.get(f"/market/swaps/{record_id}")
    assert result.status_code == 200
    assert result.json()["status"] == "SUBMITTED"
    assert result.json()["proposalHttpAcceptedAt"] is not None
    assert "secret-jwt" not in result.text
    assert "private-key" not in result.text
    assert client.get("/market/swaps/999999999").status_code == 404


def test_market_admin_does_not_make_existing_offer_writes_public():
    assert client.post("/market/offers/unknown/withdraw").status_code == 401


def test_local_offers_only_returns_own_offers_and_filters_cycle():
    now = datetime.now(timezone.utc)
    local_cycle = f"e2057-{uuid4()}"
    other_cycle = f"e2057-{uuid4()}"
    with Session(engine) as session:
        for cycle, local in [(local_cycle, True), (local_cycle, False), (other_cycle, True)]:
            session.add(MarketOffer(
                offer_id=str(uuid4()), cycle_id=cycle,
                city_id="KLD" if local else "MET", is_local=local,
                initial_quantity=100, remaining_quantity=70,
                price_per_energy=Decimal("9.50"), status="ACTIVE",
                published_at=now, last_updated_at=now,
            ))
        session.commit()
    all_local = client.get("/market/offers").json()
    assert all_local["total"] == 2
    subset = client.get("/market/offers", params={"cycleId": local_cycle}).json()
    assert subset["total"] == 1
    assert subset["items"][0]["cityId"] == "KLD"
    assert subset["items"][0]["remaining"] == 70
