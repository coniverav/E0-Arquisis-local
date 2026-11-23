"""E3-036: pruebas de falla/recuperación de outbox en PostgreSQL aislado.

PRECAUCIÓN: master/tests/conftest.py ejecuta TRUNCATE en la base de pruebas.
DATABASE_URL debe apuntar a una base exclusiva energyshark_test_*.
No conecta a RabbitMQ ni a AWS: se simulan confirms y fallos entre commits.
"""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.database import engine
from app.models import Cycle, OutboundMessage
from app.services.outbox_claims import claim_batch, finish_claim
from app.services.outbox_store import enqueue_broker, enqueue_saga_start

T0 = datetime(2026, 11, 23, 12, tzinfo=timezone.utc)
MACHINE = "arn:aws:states:us-east-1:123456789012:stateMachine:energyshark-seller"


def new_envelope(*, cycle_id=None, msg_id=None, idpk=None):
    cycle_id = cycle_id or f"cycle-{uuid4()}"
    return {
        "msgId": msg_id or str(uuid4()),
        "idpk": idpk or str(uuid4()),
        "type": "offer",
        "cycleId": cycle_id,
        "cityId": "KLD",
        "timestamp": "2026-11-23T12:00:00Z",
        "data": {"offerId": f"KLD-{cycle_id}", "quantity": 25},
    }


def persist_offer(*, cycle_id=None):
    cycle_id = cycle_id or f"cycle-{uuid4()}"
    with Session(engine) as session:
        session.add(Cycle(cycle_id=cycle_id, created_at=T0))
        record = enqueue_broker(
            session, envelope=new_envelope(cycle_id=cycle_id),
            exchange_name="market", routing_key="market.offer.KLD", now=T0,
        )
        details = (record.id, record.msg_id, record.idpk, dict(record.payload))
        session.commit()
    return cycle_id, details


def claim_one(*, worker="relay-A", now=T0, lease_seconds=30):
    with Session(engine) as session:
        rows = claim_batch(session, worker_id=worker, now=now,
                           lease_seconds=lease_seconds, limit=1, kinds=("BROKER",))
        data = [(r.id, r.claim_token, r.msg_id, r.idpk, dict(r.payload)) for r in rows]
        session.commit()
    return data


def finish_one(row_id, token, *, status, now, retry_at=None, error=None):
    with Session(engine) as session:
        success = finish_claim(session, row_id=row_id, claim_token=token,
                               outcome=status, error=error, retry_at=retry_at, now=now)
        session.commit()
    return success


def test_crash_after_commit_keeps_business_and_intention():
    # Simulación: el proceso muere tras el commit, antes de llamar al broker.
    cycle_id, (row_id, msg_id, idpk, envelope) = persist_offer()
    with Session(engine) as recovered:
        assert recovered.get(Cycle, cycle_id) is not None
        row = recovered.get(OutboundMessage, row_id)
        assert row is not None
        assert row.status == "PENDING"
        assert (row.msg_id, row.idpk, row.payload) == (msg_id, idpk, envelope)
    pending = claim_one()
    assert [(x[0], x[2], x[3], x[4]) for x in pending] == [
        (row_id, msg_id, idpk, envelope)
    ]
    assert finish_one(row_id, pending[0][1], status="PUBLISHED",
                      now=T0 + timedelta(seconds=1))
    with Session(engine) as session:
        published = session.get(OutboundMessage, row_id)
        assert published.status == "PUBLISHED"
        assert published.dispatch_required is False
        assert published.payload == envelope


def test_rollback_removes_business_and_outbox_together():
    cycle_id = f"rollback-{uuid4()}"
    with Session(engine) as session:
        session.add(Cycle(cycle_id=cycle_id, created_at=T0))
        row = enqueue_broker(
            session, envelope=new_envelope(cycle_id=cycle_id),
            exchange_name="market", routing_key="market.offer.KLD", now=T0,
        )
        row_id = row.id
        session.rollback()
    with Session(engine) as session:
        assert session.get(Cycle, cycle_id) is None
        assert session.get(OutboundMessage, row_id) is None


def test_broker_down_retries_same_stored_envelope_not_new_message():
    _, (row_id, msg_id, idpk, envelope) = persist_offer()
    first = claim_one(now=T0)[0]
    assert finish_one(row_id, first[1], status="FAILED",
                      error="BROKER_UNREACHABLE",
                      now=T0 + timedelta(seconds=1),
                      retry_at=T0 + timedelta(seconds=5))
    assert claim_one(worker="relay-B", now=T0 + timedelta(seconds=4)) == []
    second = claim_one(worker="relay-B", now=T0 + timedelta(seconds=6))[0]
    assert second[0] == row_id
    assert (second[2], second[3], second[4]) == (msg_id, idpk, envelope)
    assert finish_one(row_id, second[1], status="PUBLISHED",
                      now=T0 + timedelta(seconds=7))
    with Session(engine) as session:
        recovered = session.get(OutboundMessage, row_id)
        assert recovered.attempt_count == 2
        assert recovered.status == "PUBLISHED"
        assert recovered.last_error is None


def test_lost_ack_commit_reclaims_same_intent_after_lease():
    # ACK pudo ocurrir en RabbitMQ, pero el backend no guardó su resultado.
    # En un modelo al-menos-una-vez la nueva entrega conserva los mismos IDs.
    _, (row_id, msg_id, idpk, envelope) = persist_offer()
    first = claim_one(worker="relay-A", now=T0, lease_seconds=10)[0]
    assert claim_one(worker="relay-B", now=T0 + timedelta(seconds=5)) == []
    second = claim_one(worker="relay-B", now=T0 + timedelta(seconds=11))[0]
    assert (first[0], first[2], first[3], first[4]) == (row_id, msg_id, idpk, envelope)
    assert (second[0], second[2], second[3], second[4]) == (row_id, msg_id, idpk, envelope)
    assert second[1] != first[1]
    assert not finish_one(row_id, first[1], status="PUBLISHED",
                          now=T0 + timedelta(seconds=12))
    assert finish_one(row_id, second[1], status="PUBLISHED",
                      now=T0 + timedelta(seconds=12))
    with Session(engine) as session:
        row = session.get(OutboundMessage, row_id)
        assert row.status == "PUBLISHED"
        assert row.attempt_count == 2
        assert session.exec(select(OutboundMessage).where(
            OutboundMessage.msg_id == msg_id)).all() == [row]


def test_same_saga_intention_cannot_be_created_twice():
    idpk = str(uuid4())
    payload = {"role": "SELLER", "idpk": idpk, "cycleId": "cycle-e3-036"}
    with Session(engine) as session:
        row = enqueue_saga_start(
            session, idpk=idpk, role="SELLER", cycle_id="cycle-e3-036",
            state_machine_arn=MACHINE, execution_input=payload, now=T0,
        )
        row_id, original_input = row.id, row.execution_input
        session.commit()
    with Session(engine) as session, pytest.raises(IntegrityError):
        enqueue_saga_start(
            session, idpk=idpk, role="SELLER", cycle_id="cycle-e3-036",
            state_machine_arn=MACHINE, execution_input=payload, now=T0,
        )
        session.commit()
    with Session(engine) as session:
        row = session.get(OutboundMessage, row_id)
        assert row.status == "PENDING"
        assert row.execution_name == idpk
        assert row.execution_input == original_input
