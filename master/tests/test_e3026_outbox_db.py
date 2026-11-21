"""Pruebas de integración PostgreSQL E3-025/E3-026.

Se ejecutan SOLO con DATABASE_URL apuntando a energyshark_test_*.
El conftest existente ejecuta migraciones y TRUNCATE en dicha base.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError, DBAPIError
from sqlmodel import Session, select

from app.database import engine
from app.models import Cycle, OutboundMessage
from app.services.outbox_claims import claim_batch, finish_claim
from app.services.outbox_store import enqueue_broker, enqueue_saga_start

T0 = datetime(2026, 11, 21, 12, tzinfo=timezone.utc)
ARN = "arn:aws:states:us-east-2:123456789012:stateMachine:energyshark-seller"


def offer_event(*, kind="offer", cycle="cycle-e3026", msg_id=None, idpk=None):
    return {
        "msgId": msg_id or str(uuid4()), "idpk": idpk or str(uuid4()),
        "type": kind, "cycleId": cycle, "cityId": "KLD",
        "timestamp": "2026-11-21T12:00:00Z",
        "data": {"offerId": f"KLD-{cycle}", "quantity": 100},
    }


def add_broker(session, *, kind="offer", cycle="cycle-e3026", expires_at=None):
    return enqueue_broker(
        session, envelope=offer_event(kind=kind, cycle=cycle),
        exchange_name="market", routing_key="market.offer.KLD",
        expires_at=expires_at, now=T0,
    )


def test_business_and_outbox_commit_are_atomic():
    cycle_id = f"e3026-{uuid4()}"
    with Session(engine) as session:
        session.add(Cycle(cycle_id=cycle_id))
        row = add_broker(session, cycle=cycle_id)
        saved_id = row.id
        session.rollback()
    with Session(engine) as session:
        assert session.get(Cycle, cycle_id) is None
        assert session.get(OutboundMessage, saved_id) is None

    with Session(engine) as session:
        session.add(Cycle(cycle_id=cycle_id))
        row = add_broker(session, cycle=cycle_id)
        saved_id = row.id
        session.commit()
    with Session(engine) as session:
        assert session.get(Cycle, cycle_id) is not None
        assert session.get(OutboundMessage, saved_id) is not None


def test_old_e2_broker_rows_default_to_broker_destination():
    with Session(engine) as session:
        event = offer_event()
        row = OutboundMessage(
            msg_id=event["msgId"], idpk=event["idpk"],
            message_type="offer", cycle_id=event["cycleId"],
            payload=event, exchange_name="market", routing_key="market.offer.KLD",
            status="PENDING", created_at=T0, dispatch_required=True,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        assert row.delivery_kind == "BROKER"
        assert row.execution_name is None


def test_saga_persisted_identity_input_and_uniqueness():
    idpk = str(uuid4())
    text = '{ "role": "SELLER", "idpk": "' + idpk + '", "cycleId": "cycle-e3026" }'
    with Session(engine) as session:
        row = enqueue_saga_start(
            session, idpk=idpk, role="SELLER", state_machine_arn=ARN,
            execution_input=text, cycle_id="cycle-e3026", now=T0,
        )
        row_id, msg_id = row.id, row.msg_id
        session.commit()
    with Session(engine) as session:
        persisted = session.get(OutboundMessage, row_id)
        assert persisted.execution_name == idpk
        assert persisted.execution_input == text
        assert persisted.msg_id == msg_id
        assert persisted.delivery_kind == "STEP_FUNCTIONS"
        assert persisted.status == "PENDING"
    with Session(engine) as session, pytest.raises(IntegrityError):
        enqueue_saga_start(
            session, idpk=idpk, role="SELLER", state_machine_arn=ARN,
            execution_input=text, cycle_id="cycle-e3026", now=T0,
        )
        session.commit()


def test_postgres_rejects_mutation_of_broker_envelope_and_identity():
    with Session(engine) as session:
        row = add_broker(session)
        msg_id, row_id = row.msg_id, row.id
        session.commit()
    with Session(engine) as session:
        row = session.get(OutboundMessage, row_id)
        row.payload = {**row.payload, "msgId": str(uuid4())}
        with pytest.raises(DBAPIError, match="OUTBOX_IMMUTABLE_INTENT"):
            session.flush()
        session.rollback()
    with Session(engine) as session:
        row = session.get(OutboundMessage, row_id)
        row.idpk = str(uuid4())
        with pytest.raises(DBAPIError, match="OUTBOX_IMMUTABLE_INTENT"):
            session.flush()
        session.rollback()
    with Session(engine) as session:
        assert session.get(OutboundMessage, row_id).msg_id == msg_id


def test_claims_are_disjoint_when_two_sessions_overlap():
    with Session(engine) as session:
        ids = [add_broker(session, cycle=f"cycle-{n}").id for n in range(4)]
        session.commit()

    barrier = Barrier(2)
    def worker(worker_id):
        with Session(engine) as session:
            barrier.wait(timeout=10)
            rows = claim_batch(session, worker_id=worker_id, limit=2, now=T0)
            found = [r.id for r in rows]
            session.commit()
            return found

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker, f"relay-{i}") for i in range(2)]
        results = [future.result(timeout=15) for future in futures]
    assert not (set(results[0]) & set(results[1]))
    assert set(results[0] + results[1]) == set(ids)


def test_locked_row_is_skipped_not_blocked():
    with Session(engine) as session:
        first = add_broker(session, cycle="cycle-x").id
        second = add_broker(session, cycle="cycle-y").id
        session.commit()
    with Session(engine) as one, Session(engine) as two:
        a = claim_batch(one, worker_id="A", limit=1, now=T0)
        assert [r.id for r in a] == [first]
        # primera transacción sigue sin commit: skip_locked debe tomar la otra
        b = claim_batch(two, worker_id="B", limit=1, now=T0)
        assert [r.id for r in b] == [second]
        one.commit()
        two.commit()


def test_lease_recovery_fencing_and_identifiers_preserved():
    with Session(engine) as session:
        row = add_broker(session)
        row_id, idpk, msg_id, original_payload = row.id, row.idpk, row.msg_id, dict(row.payload)
        session.commit()
    with Session(engine) as session:
        a = claim_batch(session, worker_id="A", lease_seconds=20, now=T0)
        first_token = a[0].claim_token
        session.commit()
    with Session(engine) as session:
        assert claim_batch(session, worker_id="B", now=T0 + timedelta(seconds=10)) == []
        session.rollback()
    with Session(engine) as session:
        b = claim_batch(session, worker_id="B", now=T0 + timedelta(seconds=21))
        second_token = b[0].claim_token
        assert second_token != first_token
        session.commit()
    with Session(engine) as session:
        assert finish_claim(session, row_id=row_id, claim_token=first_token,
                            outcome="PUBLISHED", now=T0 + timedelta(seconds=22)) is False
        assert finish_claim(session, row_id=row_id, claim_token=second_token,
                            outcome="PUBLISHED", now=T0 + timedelta(seconds=22)) is True
        session.commit()
    with Session(engine) as session:
        record = session.get(OutboundMessage, row_id)
        assert record.status == "PUBLISHED" and record.dispatch_required is False
        assert record.idpk == idpk and record.msg_id == msg_id
        assert record.payload == original_payload
        assert record.attempt_count == 2
        assert record.published_at == T0 + timedelta(seconds=22)


def test_failed_claim_retries_without_rewriting_payload_or_msg_id():
    with Session(engine) as session:
        row = add_broker(session)
        original = (row.msg_id, row.idpk, dict(row.payload))
        row_id = row.id
        session.commit()
    with Session(engine) as session:
        a = claim_batch(session, worker_id="A", now=T0)
        token = a[0].claim_token
        assert finish_claim(session, row_id=row_id, claim_token=token, outcome="FAILED",
                            error="BROKER_UNREACHABLE", now=T0 + timedelta(seconds=1),
                            retry_at=T0 + timedelta(seconds=5)) is True
        session.commit()
    with Session(engine) as session:
        assert claim_batch(session, worker_id="B", now=T0 + timedelta(seconds=4)) == []
        b = claim_batch(session, worker_id="B", now=T0 + timedelta(seconds=6))
        assert [row.id for row in b] == [row_id]
        assert (b[0].msg_id, b[0].idpk, b[0].payload) == original
        session.commit()


def test_market_updates_do_not_overtake_original_offer():
    with Session(engine) as session:
        a = add_broker(session, kind="offer")
        b = add_broker(session, kind="offer-updated")
        first_id, second_id = a.id, b.id
        session.commit()
    with Session(engine) as session:
        rows = claim_batch(session, worker_id="A", now=T0)
        assert [r.id for r in rows] == [first_id]
        token = rows[0].claim_token
        session.commit()
    with Session(engine) as session:
        assert claim_batch(session, worker_id="B", now=T0) == []
        assert finish_claim(session, row_id=first_id, claim_token=token,
                            outcome="PUBLISHED", now=T0 + timedelta(seconds=1))
        session.commit()
    with Session(engine) as session:
        rows = claim_batch(session, worker_id="B", now=T0 + timedelta(seconds=2))
        assert [r.id for r in rows] == [second_id]
        session.commit()


def test_expired_row_is_not_claimed():
    with Session(engine) as session:
        add_broker(session, expires_at=T0 + timedelta(seconds=3))
        session.commit()
    with Session(engine) as session:
        assert claim_batch(session, worker_id="A", now=T0 + timedelta(seconds=4)) == []


def test_start_saga_claim_only_finishes_as_started():
    bid_idpk = str(uuid4())
    with Session(engine) as session:
        row = enqueue_saga_start(
            session, idpk=bid_idpk, role="BUYER", state_machine_arn=ARN,
            execution_input={"role": "BUYER", "idpk": bid_idpk, "cycleId": "cycle-3"}, cycle_id="cycle-3", now=T0,
        )
        row_id = row.id
        session.commit()
    with Session(engine) as session:
        claims = claim_batch(session, worker_id="step-relay", now=T0, kinds=("STEP_FUNCTIONS",))
        assert [r.id for r in claims] == [row_id]
        token = claims[0].claim_token
        with pytest.raises(ValueError, match="PUBLISHED_ONLY_FOR_BROKER"):
            finish_claim(session, row_id=row_id, claim_token=token, outcome="PUBLISHED", now=T0)
        assert finish_claim(session, row_id=row_id, claim_token=token, outcome="STARTED", now=T0)
        session.commit()
    with Session(engine) as session:
        row = session.get(OutboundMessage, row_id)
        assert row.status == "STARTED" and row.dispatch_required is False
        assert row.completed_at == T0


def test_retries_may_share_idpk_but_not_msg_id():
    idpk = str(uuid4())
    with Session(engine) as session:
        one = enqueue_broker(
            session, envelope=offer_event(idpk=idpk), exchange_name="market",
            routing_key="market.offer.KLD", now=T0,
        )
        two = enqueue_broker(
            session, envelope=offer_event(idpk=idpk), exchange_name="market",
            routing_key="market.offer.KLD", now=T0,
        )
        assert one.msg_id != two.msg_id and one.idpk == two.idpk
        session.commit()
    with Session(engine) as session:
        rows = session.exec(select(OutboundMessage).where(OutboundMessage.idpk == idpk)).all()
        assert len(rows) == 2


def test_duplicate_msg_id_rejected_by_existing_e2_constraint():
    envelope = offer_event()
    with Session(engine) as session:
        enqueue_broker(session, envelope=envelope, exchange_name="market",
                       routing_key="market.offer.KLD", now=T0)
        session.commit()
    with Session(engine) as session, pytest.raises(IntegrityError):
        enqueue_broker(session, envelope=envelope, exchange_name="market",
                       routing_key="market.offer.KLD", now=T0)
        session.commit()


def test_saga_immutable_execution_input():
    idpk = str(uuid4())
    with Session(engine) as session:
        row = enqueue_saga_start(
            session, idpk=idpk, role="SELLER", state_machine_arn=ARN,
            execution_input={"idpk": idpk, "cycleId": "cycle-4", "role": "SELLER"},
            cycle_id="cycle-4", now=T0,
        )
        row_id = row.id
        session.commit()
    with Session(engine) as session:
        row = session.get(OutboundMessage, row_id)
        row.execution_input = '{"role":"BUYER"}'
        with pytest.raises(DBAPIError, match="OUTBOX_IMMUTABLE_INTENT"):
            session.flush()
        session.rollback()


def test_invalid_saga_input_cannot_be_enqueued():
    with Session(engine) as session:
        with pytest.raises(ValueError, match="SAGA_INPUT_MUST_MATCH_INTENT"):
            enqueue_saga_start(
                session, idpk=str(uuid4()), role="SELLER", state_machine_arn=ARN,
                execution_input={"role": "SELLER", "cycleId": "cycle-5", "idpk": "wrong"},
                cycle_id="cycle-5", now=T0,
            )
        session.rollback()


def test_future_available_at_prevents_early_claim():
    with Session(engine) as session:
        envelope = offer_event()
        row = enqueue_broker(
            session, envelope=envelope, exchange_name="market", routing_key="market.offer.KLD",
            available_at=T0 + timedelta(seconds=15), now=T0,
        )
        row_id = row.id
        session.commit()
    with Session(engine) as session:
        assert claim_batch(session, worker_id="A", now=T0 + timedelta(seconds=14)) == []
        rows = claim_batch(session, worker_id="A", now=T0 + timedelta(seconds=15))
        assert [row.id for row in rows] == [row_id]
        session.commit()
