from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlmodel import Session, select

from app.database import engine
from app.models import SagaExecution, SagaTaskToken
from app.services.saga_tokens import (
    SagaCorrelationError,
    attach_execution_arn,
    attach_swap_id,
    complete_task_token,
    expire_waiting_task_tokens,
    find_waiting_task_token,
    get_or_create_saga_execution,
    mark_saga_terminal,
    register_task_token,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_execution(session: Session, *, role: str = "SELLER") -> SagaExecution:
    now = _now()
    execution, created = get_or_create_saga_execution(
        session,
        idpk=str(uuid4()),
        role=role,
        cycle_id="cycle-e3",
        timeout_at=now + timedelta(minutes=10),
        now=now,
    )
    assert created
    return execution


def test_saga_execution_replay_is_idempotent_and_rejects_conflicts():
    now = _now()
    idpk = str(uuid4())
    timeout_at = now + timedelta(minutes=10)
    with Session(engine) as session:
        execution, created = get_or_create_saga_execution(
            session,
            idpk=idpk,
            role="SELLER",
            cycle_id="cycle-e3",
            timeout_at=timeout_at,
            now=now,
        )
        attach_execution_arn(
            session,
            execution,
            execution_arn="arn:aws:states:local:000:execution:seller:bid-1",
            now=now,
        )
        session.commit()

        replay, created = get_or_create_saga_execution(
            session,
            idpk=idpk,
            role="SELLER",
            cycle_id="cycle-e3",
            timeout_at=timeout_at,
            now=now,
        )
        assert not created
        assert replay.id == execution.id
        assert replay.execution_arn.endswith(":bid-1")

        with pytest.raises(SagaCorrelationError):
            get_or_create_saga_execution(
                session,
                idpk=idpk,
                role="BUYER",
                cycle_id="cycle-e3",
                timeout_at=timeout_at,
                now=now,
            )


def test_callback_lookup_prefers_idpk_and_falls_back_to_swap_id():
    now = _now()
    with Session(engine) as session:
        execution = _new_execution(session)
        attach_execution_arn(
            session,
            execution,
            execution_arn="arn:aws:states:local:000:execution:seller:bid-2",
            now=now,
        )
        attach_swap_id(session, execution, swap_id="swap-e3", now=now)
        proposal, _ = register_task_token(
            session,
            execution,
            task_token="proposal-token",
            wait_type="PROPOSAL",
            correlation_idpk="operation-idpk",
            timeout_at=now + timedelta(minutes=2),
            now=now,
        )
        terminal, _ = register_task_token(
            session,
            execution,
            task_token="terminal-token",
            wait_type="TERMINAL",
            correlation_idpk="accept-idpk",
            swap_id="swap-e3",
            timeout_at=now + timedelta(minutes=5),
            now=now,
        )
        session.commit()

        assert find_waiting_task_token(
            session,
            correlation_idpk="operation-idpk",
            swap_id="swap-e3",
            role="SELLER",
            wait_type="PROPOSAL",
            now=now,
        ).id == proposal.id
        assert find_waiting_task_token(
            session,
            correlation_idpk="unknown-idpk",
            swap_id="swap-e3",
            role="SELLER",
            execution_arn=execution.execution_arn,
            wait_type="TERMINAL",
            now=now,
        ).id == terminal.id
        assert find_waiting_task_token(
            session,
            swap_id="swap-e3",
            role="BUYER",
            now=now,
        ) is None


def test_new_token_supersedes_previous_wait_and_timeout_does_not_infer_result():
    now = _now()
    with Session(engine) as session:
        execution = _new_execution(session, role="BUYER")
        first, _ = register_task_token(
            session,
            execution,
            task_token="first-token",
            wait_type="TERMINAL",
            swap_id="swap-retry",
            timeout_at=now + timedelta(seconds=5),
            now=now,
        )
        second, _ = register_task_token(
            session,
            execution,
            task_token="second-token",
            wait_type="TERMINAL",
            swap_id="swap-retry",
            timeout_at=now + timedelta(seconds=10),
            now=now + timedelta(seconds=1),
        )
        session.commit()
        session.refresh(first)
        assert first.status == "SUPERSEDED"
        assert second.status == "WAITING"

        expired = expire_waiting_task_tokens(
            session,
            now=now + timedelta(seconds=11),
        )
        session.commit()
        assert second.id in expired
        session.refresh(second)
        session.refresh(execution)
        assert second.status == "EXPIRED"
        # Expirar un callback no inventa una liquidación ni cierra la saga.
        assert execution.status == "WAITING"
        assert find_waiting_task_token(
            session,
            swap_id="swap-retry",
            now=now + timedelta(seconds=11),
        ) is None


def test_callback_and_terminal_transitions_are_applied_once():
    now = _now()
    with Session(engine) as session:
        execution = _new_execution(session)
        token, _ = register_task_token(
            session,
            execution,
            task_token="callback-token",
            wait_type="TERMINAL",
            swap_id="swap-terminal",
            timeout_at=now + timedelta(minutes=5),
            now=now,
        )
        assert complete_task_token(
            session,
            token,
            status="SUCCEEDED",
            callback_idpk="event-idpk",
            now=now,
        )
        assert not complete_task_token(
            session,
            token,
            status="SUCCEEDED",
            callback_idpk="event-idpk",
            now=now,
        )
        assert mark_saga_terminal(
            session,
            execution,
            status="SETTLED",
            now=now,
        )
        assert not mark_saga_terminal(
            session,
            execution,
            status="FAILED",
            now=now + timedelta(seconds=1),
        )
        session.commit()

        stored_tokens = session.exec(select(SagaTaskToken)).all()
        stored_executions = session.exec(select(SagaExecution)).all()
        assert len(stored_tokens) == 1
        assert stored_tokens[0].callback_idpk == "event-idpk"
        assert "callback-token" not in repr(stored_tokens[0])
        assert "task_token" not in stored_tokens[0].model_dump()
        assert len(stored_executions) == 1
        assert stored_executions[0].status == "SETTLED"
