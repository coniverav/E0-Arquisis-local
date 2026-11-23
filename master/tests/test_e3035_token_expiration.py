"""E3-035: correlación de callbacks y expiración durable de task tokens.

Requiere la base exclusiva energyshark_test_* y migraciones (conftest.py).
"""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlmodel import Session, select

from app.database import engine
from app.models import SagaTaskToken
from app.services.saga_tokens import (
    complete_task_token,
    expire_waiting_task_tokens,
    find_waiting_task_token,
    get_or_create_saga_execution,
    register_task_token,
)

T0 = datetime(2026, 11, 23, 12, tzinfo=timezone.utc)


def test_waiting_token_expires_and_late_event_cannot_resume_it():
    idpk = str(uuid4())
    with Session(engine) as session:
        execution, created = get_or_create_saga_execution(
            session, idpk=idpk, role="SELLER", cycle_id="cycle-token-test",
            timeout_at=T0 + timedelta(minutes=5), now=T0,
        )
        assert created
        token, token_created = register_task_token(
            session, execution, task_token=f"opaque-{uuid4()}",
            wait_type="PROPOSAL", correlation_idpk=idpk,
            timeout_at=T0 + timedelta(seconds=15), now=T0,
        )
        token_id = token.id
        assert token_created
        session.commit()
    with Session(engine) as session:
        found = find_waiting_task_token(
            session, correlation_idpk=idpk,
            role="SELLER", wait_type="PROPOSAL",
            now=T0 + timedelta(seconds=10),
        )
        assert found is not None and found.id == token_id
        assert expire_waiting_task_tokens(session, now=T0 + timedelta(seconds=16)) == [token_id]
        session.commit()
    with Session(engine) as session:
        assert find_waiting_task_token(session, correlation_idpk=idpk,
                                       now=T0 + timedelta(seconds=17)) is None
        expired = session.get(SagaTaskToken, token_id)
        assert expired.status == "EXPIRED"
        assert not complete_task_token(session, expired, status="SUCCEEDED",
                                       callback_idpk=str(uuid4()),
                                       now=T0 + timedelta(seconds=17))
        session.commit()
    with Session(engine) as session:
        assert session.get(SagaTaskToken, token_id).status == "EXPIRED"


def test_duplicate_callback_completes_token_only_once():
    idpk = str(uuid4())
    with Session(engine) as session:
        execution, _ = get_or_create_saga_execution(
            session, idpk=idpk, role="BUYER", cycle_id="cycle-buyer-test",
            timeout_at=T0 + timedelta(minutes=4), now=T0,
        )
        token, _ = register_task_token(
            session, execution, task_token=f"opaque-{uuid4()}",
            wait_type="TERMINAL", swap_id=str(uuid4()),
            timeout_at=T0 + timedelta(minutes=2), now=T0,
        )
        token_id = token.id
        assert complete_task_token(session, token, status="SUCCEEDED",
                                   callback_idpk=str(uuid4()), now=T0 + timedelta(seconds=4))
        assert not complete_task_token(session, token, status="FAILED",
                                       callback_idpk=str(uuid4()), now=T0 + timedelta(seconds=5))
        session.commit()
    with Session(engine) as session:
        assert session.get(SagaTaskToken, token_id).status == "SUCCEEDED"
