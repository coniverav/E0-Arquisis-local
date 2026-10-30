"""E2-062 / AD6: edición sin redeploy, auditoría y vigencia por ciclo."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.auth import verify_jwt
from app.database import engine
from app.main import app
from app.models import Cycle, StrategyConfig
from app.services.strategy_config import (
    StrategyVersionConflict, bind_strategy_to_cycle, create_pending_strategy,
    get_cycle_strategy,
)
from app.services.seller_offer import calculate_offer


client = TestClient(app)
POLICY = {
    "offerPriceMarkup": "1.1700",
    "offerQuantityFraction": "0.8000",
    "bidAcceptancePolicy": "MAX_REMAINING",
    "offerSelectionPolicy": "MIN_DISTANCE",
    "spanSeconds": 45,
}


def _authorized_post(payload, *, actor="operator-e2062"):
    app.dependency_overrides[verify_jwt] = lambda: {"sub": actor}
    try:
        return client.post("/market/strategy", json=payload)
    finally:
        app.dependency_overrides.pop(verify_jwt, None)


def _authorized_get(path, params=None):
    app.dependency_overrides[verify_jwt] = lambda: {"sub": "operator-e2062"}
    try:
        return client.get(path, params=params)
    finally:
        app.dependency_overrides.pop(verify_jwt, None)


def _cycle(session, at):
    current = Cycle(cycle_id=f"e2062-{uuid4()}", created_at=at,
                    market_opens_at=at)
    session.add(current)
    session.flush()
    return current


def test_options_exposes_validated_polices_without_db_mutation():
    result = client.get("/market/strategy/options")
    assert result.status_code == 200
    assert result.json() == {
        "bidAcceptancePolicies": ["BATCH", "FIFO", "MAX_REMAINING"],
        "offerSelectionPolicies": ["CHEAPEST_EFFECTIVE_PRICE", "MIN_DISTANCE"],
        "minSpanSeconds": 0, "decimalPlaces": 4,
    }
    with Session(engine) as session:
        assert session.exec(select(StrategyConfig)).all() == []


def test_audit_requires_auth_and_does_not_create_initial_configuration():
    assert client.get("/market/strategy/audit").status_code == 401
    audited = _authorized_get("/market/strategy/audit")
    assert audited.status_code == 200
    assert audited.json() == {"total": 0, "offset": 0, "limit": 50, "items": []}
    with Session(engine) as session:
        assert session.exec(select(StrategyConfig)).all() == []


def test_update_requires_claim_subject_and_refuses_invalid_values_without_writes():
    assert client.post("/market/strategy", json=POLICY).status_code == 401
    assert _authorized_post(POLICY, actor=" ").status_code == 403
    bad_requests = [
        {**POLICY, "offerPriceMarkup": "1.12345"},
        {**POLICY, "offerPriceMarkup": "9999999"},
        {**POLICY, "offerQuantityFraction": "0.10001"},
        {**POLICY, "offerPriceOverride": "0"},
        {**POLICY, "offerPriceOverride": "2.12345"},
        {**POLICY, "offerQuantityCap": -1},
        {**POLICY, "offerQuantityCap": 1.5},
        {**POLICY, "bidAcceptancePolicy": "NO_SUCH_POLICY"},
        {**POLICY, "offerSelectionPolicy": "NO_SUCH_POLICY"},
        {**POLICY, "spanSeconds": True},
        {**POLICY, "expectedVersion": 0},
        {**POLICY, "expectedVersion": "2"},
        {**POLICY, "extra": "forbidden"},
    ]
    for invalid in bad_requests:
        response = _authorized_post(invalid)
        assert response.status_code == 422, (invalid, response.text)
    with Session(engine) as session:
        assert session.exec(select(StrategyConfig)).all() == []


def test_edition_audits_all_fields_actor_and_preserves_exact_values():
    result = _authorized_post(POLICY)
    assert result.status_code == 201, result.text
    body = result.json()
    assert body["version"] == 2 and body["status"] == "PENDING"
    assert body["createdBy"] == "operator-e2062"
    assert body["offerPriceMarkup"] == "1.1700"
    assert body["offerQuantityFraction"] == "0.8000"
    assert body["bidAcceptancePolicy"] == "MAX_REMAINING"
    assert body["offerSelectionPolicy"] == "MIN_DISTANCE"
    assert body["spanSeconds"] == 45
    with Session(engine) as other_connection:
        versions = other_connection.exec(select(StrategyConfig).order_by(StrategyConfig.version)).all()
        assert [v.version for v in versions] == [1, 2]
        assert versions[0].status == "ACTIVE"
        assert versions[1].offer_price_markup == Decimal("1.1700")
        assert versions[1].created_by == "operator-e2062"
    audit = _authorized_get("/market/strategy/audit").json()
    assert audit["total"] == 2
    item = audit["items"][0]
    assert item["version"] == 2 and item["previousVersion"] == 1
    assert item["createdBy"] == "operator-e2062"
    assert item["effectiveFromCycleId"] is None
    assert item["changes"] == {
        "offerPriceMarkup": {"before": "1.2000", "after": "1.1700"},
        "offerQuantityFraction": {"before": "1.0000", "after": "0.8000"},
        "bidAcceptancePolicy": {"before": "FIFO", "after": "MAX_REMAINING"},
        "offerSelectionPolicy": {"before": "CHEAPEST_EFFECTIVE_PRICE", "after": "MIN_DISTANCE"},
        "spanSeconds": {"before": 60, "after": 45},
    }


def test_direct_offer_controls_are_versioned_and_apply_only_to_next_cycle():
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        current = _cycle(session, now - timedelta(minutes=1))
        old = bind_strategy_to_cycle(session, current)
        session.commit()
        current_id = current.cycle_id
        assert old.offer_price_override is None
    saved = _authorized_post({**POLICY, "offerPriceOverride": "330.0000", "offerQuantityCap": 150})
    assert saved.status_code == 201, saved.text
    assert saved.json()["offerPriceOverride"] == "330.0000"
    assert saved.json()["offerQuantityCap"] == 150
    with Session(engine) as session:
        upcoming = _cycle(session, now + timedelta(minutes=1))
        selected = bind_strategy_to_cycle(session, upcoming)
        session.commit()
        assert get_cycle_strategy(session, current_id).offer_price_override is None
        assert selected.offer_price_override == Decimal("330.0000")
        assert selected.offer_quantity_cap == 150
        quantity, price = calculate_offer(
            sellable=500, wallet_available=Decimal("400"),
            generation_cost=Decimal("200"), buyback_price=Decimal("250"),
            markup=selected.offer_price_markup, fraction=selected.offer_quantity_fraction,
            price_override=selected.offer_price_override,
            quantity_cap=selected.offer_quantity_cap,
        )
        assert (quantity, price) == (150, Decimal("330.00"))
    audit = _authorized_get("/market/strategy/audit").json()["items"][0]
    assert audit["changes"]["offerPriceOverride"] == {"before": None, "after": "330.0000"}
    assert audit["changes"]["offerQuantityCap"] == {"before": None, "after": 150}
    assert calculate_offer(
        sellable=500, wallet_available=Decimal("400"),
        generation_cost=Decimal("200"), buyback_price=Decimal("250"),
        price_override=Decimal("240"), quantity_cap=0,
    ) == (0, Decimal("250.00"))


def test_audit_pagination_compares_against_previous_version_outside_page():
    assert _authorized_post(POLICY).status_code == 201
    second = {**POLICY, "spanSeconds": 120, "expectedVersion": 2}
    assert _authorized_post(second).status_code == 201
    audit_latest = _authorized_get("/market/strategy/audit", {"limit": 1}).json()
    assert audit_latest["total"] == 3
    assert audit_latest["items"][0]["version"] == 3
    assert audit_latest["items"][0]["changes"] == {
        "spanSeconds": {"before": 45, "after": 120}
    }
    older = _authorized_get("/market/strategy/audit", {"offset": 1, "limit": 1}).json()
    assert older["items"][0]["version"] == 2
    assert older["items"][0]["changes"]["offerPriceMarkup"]["before"] == "1.2000"
    assert _authorized_get("/market/strategy/audit", {"limit": 201}).status_code == 422


def test_stale_edit_returns_409_and_does_not_create_version():
    assert _authorized_post(POLICY).status_code == 201
    first = _authorized_post({**POLICY, "spanSeconds": 35, "expectedVersion": 1})
    assert first.status_code == 409
    assert first.json()["detail"] == {
        "code": "STALE_STRATEGY_VERSION", "expectedVersion": 1, "currentVersion": 2
    }
    assert _authorized_post({**POLICY, "spanSeconds": 36, "expectedVersion": 2}).status_code == 201
    with Session(engine) as session:
        assert [s.version for s in session.exec(select(StrategyConfig).order_by(StrategyConfig.version))] == [1, 2, 3]


def test_ciclo_abierto_usa_vieja_config_y_proximo_usa_cinco_parametros_nuevos():
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        current = _cycle(session, now - timedelta(minutes=2))
        initial = bind_strategy_to_cycle(session, current)
        session.commit()
        current_id = current.cycle_id
        assert initial.version == 1
    edited = _authorized_post(POLICY)
    assert edited.status_code == 201
    read = client.get("/market/strategy", params={"cycleId": current_id}).json()
    assert read["cycleStrategy"]["version"] == 1
    assert read["pending"]["version"] == 2
    with Session(engine) as session:
        upcoming = _cycle(session, datetime.now(timezone.utc) + timedelta(minutes=2))
        activated = bind_strategy_to_cycle(session, upcoming)
        session.commit()
        upcoming_id = upcoming.cycle_id
        assert activated.version == 2
    with Session(engine) as session:
        before = get_cycle_strategy(session, current_id)
        after = get_cycle_strategy(session, upcoming_id)
        assert before.version == 1 and before.span_seconds == 60
        assert after.version == 2
        assert after.offer_price_markup == Decimal("1.1700")
        assert after.offer_quantity_fraction == Decimal("0.8000")
        assert after.bid_acceptance_policy == "MAX_REMAINING"
        assert after.offer_selection_policy == "MIN_DISTANCE"
        assert after.span_seconds == 45
        quantity, price = calculate_offer(
            sellable=1000, wallet_available=Decimal("1000"),
            generation_cost=Decimal("100"), buyback_price=Decimal("100"),
            markup=after.offer_price_markup,
            fraction=after.offer_quantity_fraction,
        )
        assert quantity == 800 and price == Decimal("117.00")
    audit = _authorized_get("/market/strategy/audit").json()["items"][0]
    assert audit["status"] == "ACTIVE"
    assert audit["effectiveFromCycleId"] == upcoming_id
    assert client.get("/market/strategy", params={"cycleId": current_id}).json()["cycleStrategy"]["version"] == 1


def test_multiple_editions_before_new_cycle_only_latest_is_effective():
    assert _authorized_post(POLICY).status_code == 201
    changed = {**POLICY, "offerPriceMarkup": "1.0900", "spanSeconds": 30, "expectedVersion": 2}
    assert _authorized_post(changed).status_code == 201
    with Session(engine) as session:
        cycle = _cycle(session, datetime.now(timezone.utc) + timedelta(minutes=1))
        current = bind_strategy_to_cycle(session, cycle)
        assert current.version == 3 and current.span_seconds == 30
        session.commit()
    with Session(engine) as session:
        assert session.exec(select(StrategyConfig).where(StrategyConfig.version == 2)).one().status == "ARCHIVED"
        assert session.exec(select(StrategyConfig).where(StrategyConfig.version == 3)).one().status == "ACTIVE"
        assert session.exec(select(StrategyConfig).where(StrategyConfig.version == 1)).one().status == "ARCHIVED"


def test_audit_get_never_changes_versions_or_cycle_binding():
    assert _authorized_post(POLICY).status_code == 201
    with Session(engine) as session:
        original = [(x.version, x.status, x.effective_from_cycle_id)
                    for x in session.exec(select(StrategyConfig).order_by(StrategyConfig.version))]
    for _ in range(3):
        assert _authorized_get("/market/strategy/audit").status_code == 200
    with Session(engine) as session:
        afterwards = [(x.version, x.status, x.effective_from_cycle_id)
                      for x in session.exec(select(StrategyConfig).order_by(StrategyConfig.version))]
    assert afterwards == original


def test_concurrent_edits_share_the_same_db_version_lock():
    """Dos workers master/master2 no pueden confirmar la misma versión base."""
    assert _authorized_post(POLICY).status_code == 201  # actual = 2
    barrier = Barrier(2)

    def edit(span):
        barrier.wait(timeout=15)
        with Session(engine) as session:
            try:
                saved = create_pending_strategy(
                    session, offer_price_markup=Decimal("1.2"),
                    offer_quantity_fraction=Decimal("1"),
                    bid_acceptance_policy="FIFO",
                    offer_selection_policy="MIN_DISTANCE",
                    span_seconds=span, created_by=f"worker-{span}",
                    expected_version=2,
                )
                session.commit()
                return ("created", saved.version)
            except StrategyVersionConflict as exc:
                session.rollback()
                return ("stale", exc.actual)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(edit, 30)
        second = executor.submit(edit, 120)
        outcomes = [first.result(timeout=30), second.result(timeout=30)]
    assert sorted(outcomes) == [("created", 3), ("stale", 3)]
    with Session(engine) as session:
        assert [v.version for v in session.exec(
            select(StrategyConfig).order_by(StrategyConfig.version)
        )] == [1, 2, 3]
