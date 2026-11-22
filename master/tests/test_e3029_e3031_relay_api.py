"""Integración PostgreSQL de los relays AD8; nunca usa RabbitMQ ni AWS."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.database import engine
from app.main import app
from app.models import OutboundMessage
from app.services.outbox_store import enqueue_broker, enqueue_saga_start


NOW = datetime.now(timezone.utc)
MACHINE = "arn:aws:states:us-east-1:123456789012:stateMachine:energyshark-seller"


def offer(*, cycle_id="cycle-relay-api"):
    return {
        "msgId": str(uuid4()),
        "idpk": str(uuid4()),
        "type": "offer",
        "timestamp": NOW.isoformat(),
        "cityId": "KLD",
        "cycleId": cycle_id,
        "data": {"offerId": "KLD-relay", "quantity": 20, "pricePerEnergy": 3.5},
    }


def seed_two_destinations():
    broker = offer()
    saga_idpk = str(uuid4())
    saga_input = {"idpk": saga_idpk, "role": "SELLER", "cycleId": "cycle-relay-api"}
    with Session(engine) as session:
        broker_row = enqueue_broker(
            session,
            envelope=broker,
            exchange_name="market",
            routing_key="market.offer.KLD",
            now=NOW,
        )
        saga_row = enqueue_saga_start(
            session,
            idpk=saga_idpk,
            role="SELLER",
            state_machine_arn=MACHINE,
            execution_input=saga_input,
            cycle_id="cycle-relay-api",
            now=NOW,
        )
        result = broker_row.id, saga_row.id, broker, saga_input
        session.commit()
    return result


def claim(kind, worker="relay-a", limit=10):
    return TestClient(app).post("/internal/outbox-relay/claims", json={
        "workerId": worker,
        "deliveryKind": kind,
        "limit": limit,
        "leaseSeconds": 30,
    })


def test_destinations_are_claimed_separately_and_completed_with_fencing():
    broker_id, saga_id, original, saga_input = seed_two_destinations()

    broker_response = claim("BROKER")
    assert broker_response.status_code == 200
    broker_items = broker_response.json()["items"]
    assert len(broker_items) == 1
    broker = broker_items[0]
    assert broker["id"] == broker_id
    assert broker["payload"] == original
    assert (broker["msgId"], broker["idpk"]) == (original["msgId"], original["idpk"])
    assert "stateMachineArn" not in broker

    saga_response = claim("STEP_FUNCTIONS")
    assert saga_response.status_code == 200
    saga_items = saga_response.json()["items"]
    assert len(saga_items) == 1
    saga = saga_items[0]
    assert saga["id"] == saga_id
    assert saga["executionName"] == saga["idpk"]
    assert saga["executionInput"] == (
        '{"cycleId":"cycle-relay-api","idpk":"'
        + saga["idpk"]
        + '","role":"SELLER"}'
    )
    assert "payload" not in saga

    client = TestClient(app)
    published = client.post(f"/internal/outbox-relay/{broker_id}/result", json={
        "claimToken": broker["claimToken"],
        "outcome": "PUBLISHED",
    })
    assert published.status_code == 200
    execution_arn = MACHINE.replace(":stateMachine:", ":execution:") + f":{saga['idpk']}"
    started = client.post(f"/internal/outbox-relay/{saga_id}/result", json={
        "claimToken": saga["claimToken"],
        "outcome": "STARTED",
        "executionArn": execution_arn,
    })
    assert started.status_code == 200

    # Un callback tardío con el token ya consumido no puede reescribir estado.
    stale = client.post(f"/internal/outbox-relay/{saga_id}/result", json={
        "claimToken": saga["claimToken"],
        "outcome": "FAILED",
        "error": "late relay",
    })
    assert stale.status_code == 409

    with Session(engine) as session:
        broker_row = session.get(OutboundMessage, broker_id)
        saga_row = session.get(OutboundMessage, saga_id)
        assert broker_row.status == "PUBLISHED" and not broker_row.dispatch_required
        assert saga_row.status == "STARTED" and not saga_row.dispatch_required
        assert saga_row.execution_arn == execution_arn
        assert saga_row.execution_input == (
            '{"cycleId":"cycle-relay-api","idpk":"'
            + saga_row.idpk
            + '","role":"SELLER"}'
        )


def test_failed_relay_reclaims_same_broker_identity_after_backoff():
    broker_id, _, original, _ = seed_two_destinations()
    first = claim("BROKER").json()["items"][0]
    response = TestClient(app).post(f"/internal/outbox-relay/{broker_id}/result", json={
        "claimToken": first["claimToken"],
        "outcome": "FAILED",
        "error": "BROKER_DOWN",
        "retryAfterSeconds": 1,
    })
    assert response.status_code == 200

    with Session(engine) as session:
        row = session.get(OutboundMessage, broker_id)
        row.available_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.add(row)
        session.commit()

    second = claim("BROKER", "relay-b").json()["items"][0]
    assert second["claimToken"] != first["claimToken"]
    assert second["payload"] == original
    assert (second["msgId"], second["idpk"]) == (original["msgId"], original["idpk"])


def test_legacy_dispatch_never_returns_step_functions_intent():
    _, saga_id, _, _ = seed_two_destinations()
    response = TestClient(app).get("/internal/audit/outbound/dispatch")
    assert response.status_code == 200
    assert all(item["type"] != "SAGA_START" for item in response.json()["items"])
    with Session(engine) as session:
        saga = session.get(OutboundMessage, saga_id)
        assert saga.delivery_kind == "STEP_FUNCTIONS"
        assert saga.status == "PENDING"


def test_rate_limited_row_is_not_returned_and_remains_recoverable(monkeypatch):
    broker_id, _, original, _ = seed_two_destinations()
    monkeypatch.setattr(
        "app.routers.outbox_relay.reserve_rate_limit",
        lambda session, category, now: 7,
    )
    response = claim("BROKER")
    assert response.status_code == 200
    assert response.json() == {"items": []}
    with Session(engine) as session:
        row = session.get(OutboundMessage, broker_id)
        assert row.status == "FAILED"
        assert row.dispatch_required is True
        assert row.last_error == "OUTBOUND_RATE_LIMIT"
        assert (row.msg_id, row.idpk, row.payload) == (
            original["msgId"], original["idpk"], original,
        )
        assert row.available_at > datetime.now(timezone.utc)
        assert row.claim_token is None and row.lease_until is None


def test_two_relay_requests_claim_disjoint_batches():
    with Session(engine) as session:
        expected = []
        for index in range(4):
            payload = offer(cycle_id=f"cycle-concurrent-{index}")
            expected.append(enqueue_broker(
                session,
                envelope=payload,
                exchange_name="market",
                routing_key="market.offer.KLD",
                now=NOW,
            ).id)
        session.commit()

    barrier = Barrier(2)

    def worker(number):
        barrier.wait(timeout=10)
        response = claim("BROKER", f"relay-{number}", limit=2)
        assert response.status_code == 200
        return [item["id"] for item in response.json()["items"]]

    with ThreadPoolExecutor(max_workers=2) as pool:
        batches = list(pool.map(worker, range(2)))

    assert len(batches[0]) == len(batches[1]) == 2
    assert set(batches[0]).isdisjoint(batches[1])
    assert set(batches[0] + batches[1]) == set(expected)
