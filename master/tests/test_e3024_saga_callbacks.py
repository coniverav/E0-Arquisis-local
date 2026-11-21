"""E3-024: ledger primero y callback durable de Step Functions después."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.database import engine
from app.main import app
from app.models import (
    Cycle,
    InboundMessage,
    LedgerEntry,
    SagaTaskToken,
    SwapRecord,
)
from app.services.saga_callbacks import resolve_wallet_callback
from app.services.saga_tokens import (
    get_or_create_saga_execution,
    register_task_token,
)
from tests.contracts_v3 import example, validate


client = TestClient(app)


@pytest.fixture(autouse=True)
def _enable_saga_callbacks(monkeypatch):
    monkeypatch.setenv("E3_SAGA_CALLBACKS_ENABLED", "true")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _seed_settlement(*, token_swap_id: str | None = None) -> dict[str, str]:
    now = _now()
    cycle_id = f"e3-024-{uuid4()}"
    operation_idpk = str(uuid4())
    saga_idpk = str(uuid4())
    swap_id = f"swp-{uuid4().hex[:12]}"
    with Session(engine) as session:
        session.add(Cycle(cycle_id=cycle_id, created_at=now))
        session.add(SwapRecord(
            idpk=str(uuid4()),
            swap_id=swap_id,
            cycle_id=cycle_id,
            role="ACCEPTOR",
            counterpart_city_id="COR",
            give_asset="CR",
            give_amount=Decimal("500"),
            take_asset="KWH",
            take_amount=Decimal("20"),
            status="ACCEPT_SUBMITTED",
            idempotency_key=str(uuid4()),
            accept_idempotency_key=operation_idpk,
            created_at=now,
        ))
        execution, _ = get_or_create_saga_execution(
            session,
            idpk=saga_idpk,
            role="BUYER",
            cycle_id=cycle_id,
            swap_id=swap_id,
            timeout_at=now + timedelta(minutes=5),
            now=now,
        )
        token, _ = register_task_token(
            session,
            execution,
            task_token="task-token-e3-024",
            wait_type="TERMINAL",
            correlation_idpk=operation_idpk,
            swap_id=token_swap_id,
            timeout_at=now + timedelta(minutes=3),
            now=now,
        )
        session.commit()
        return {
            "cycle_id": cycle_id,
            "operation_idpk": operation_idpk,
            "swap_id": swap_id,
            "receipt": token.task_token_hash,
        }


def _settled_event(keys: dict[str, str]) -> dict:
    event = deepcopy(example("swap-settled.json"))
    event.update(
        idpk=str(uuid4()),
        msgId=str(uuid4()),
        timestamp=_now().isoformat(),
    )
    event["data"].update(
        idpk=keys["operation_idpk"],
        swapId=keys["swap_id"],
        proposer="COR",
        acceptor="KLD",
        give={"asset": "CR", "amount": 500},
        take={"asset": "KWH", "amount": 20},
        memo="COR-offer-e3-024",
        settledAt=_now().isoformat(),
        balancesAfter={
            "CR": {"available": 1000, "held": 0},
            "KWH": {"available": 20, "held": 0},
        },
    )
    validate("amqp/swap-settled", event)
    return event


def test_settlement_commits_once_and_callback_recovers_after_redelivery():
    keys = _seed_settlement()
    event = _settled_event(keys)

    first = client.post("/internal/messages", json=event)
    assert first.status_code == 200, first.text
    callback = first.json()["sagaCallback"]
    assert callback["output"] == {
        "outcome": "SETTLED",
        "swapId": keys["swap_id"],
    }
    assert callback["receipt"] == keys["receipt"]
    assert set(callback) == {
        "mode", "taskToken", "receipt", "output", "correlationIdpk", "swapId",
    }

    # Simula que SendTaskSuccess o el POST de resultado falló: RabbitMQ
    # reentrega, Master no repite el ledger y devuelve el mismo token pendiente.
    replay = client.post("/internal/messages", json=event)
    assert replay.status_code == 200
    assert replay.json()["status"] == "duplicate"
    assert replay.json()["sagaCallback"]["receipt"] == keys["receipt"]

    with Session(engine) as session:
        entries = session.exec(select(LedgerEntry).where(
            LedgerEntry.cycle_id == keys["cycle_id"],
            LedgerEntry.operation_type == "SWAP_SETTLED",
        )).all()
        assert len(entries) == 1
        assert entries[0].budget_delta == Decimal("-500.00")
        assert entries[0].energy_delta == Decimal("20.00")
        assert session.exec(select(SagaTaskToken)).one().status == "WAITING"

    result = client.post("/internal/saga-callbacks/result", json={
        "receipt": keys["receipt"],
        "status": "SUCCEEDED",
        "callbackIdpk": event["idpk"],
    })
    assert result.status_code == 200
    assert result.json() == {"status": "SUCCEEDED", "updated": True}

    after_completion = client.post("/internal/messages", json=event)
    assert after_completion.status_code == 200
    assert "sagaCallback" not in after_completion.json()
    with Session(engine) as session:
        assert session.exec(select(SagaTaskToken)).one().callback_idpk == event["idpk"]
        assert len(session.exec(select(LedgerEntry)).all()) == 1


def test_callback_falls_back_to_swap_id_and_permanent_failure_is_idempotent():
    keys = _seed_settlement(token_swap_id="placeholder")
    # El token se registró con una correlación idpk que no vendrá en el evento;
    # se corrige sólo el swapId para ejercitar el fallback contractual.
    with Session(engine) as session:
        token = session.exec(select(SagaTaskToken)).one()
        token.correlation_idpk = str(uuid4())
        token.swap_id = keys["swap_id"]
        session.add(token)
        session.commit()

    event = _settled_event(keys)
    response = client.post("/internal/messages", json=event)
    assert response.status_code == 200
    assert response.json()["sagaCallback"]["swapId"] == keys["swap_id"]

    body = {
        "receipt": keys["receipt"],
        "status": "FAILED",
        "callbackIdpk": event["idpk"],
    }
    first = client.post("/internal/saga-callbacks/result", json=body)
    replay = client.post("/internal/saga-callbacks/result", json=body)
    assert first.json() == {"status": "FAILED", "updated": True}
    assert replay.json() == {"status": "FAILED", "updated": False}
    assert "sagaCallback" not in client.post(
        "/internal/messages", json=event
    ).json()


def test_late_event_with_expired_token_updates_ledger_but_not_saga():
    keys = _seed_settlement()
    with Session(engine) as session:
        token = session.exec(select(SagaTaskToken)).one()
        token.timeout_at = _now() - timedelta(seconds=1)
        session.add(token)
        session.commit()

    response = client.post("/internal/messages", json=_settled_event(keys))
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert "sagaCallback" not in response.json()
    with Session(engine) as session:
        assert len(session.exec(select(LedgerEntry)).all()) == 1
        assert session.exec(select(SagaTaskToken)).one().status == "WAITING"


def test_proposed_event_selects_proposal_wait_and_never_terminal_wait():
    now = _now()
    operation_idpk = str(uuid4())
    envelope_idpk = str(uuid4())
    swap_id = f"swp-{uuid4().hex[:12]}"
    with Session(engine) as session:
        session.add(SwapRecord(
            idpk=operation_idpk,
            cycle_id="cycle-proposal",
            role="PROPOSER",
            counterpart_city_id="COR",
            give_asset="KWH",
            give_amount=Decimal("20"),
            take_asset="CR",
            take_amount=Decimal("500"),
            status="SUBMITTED",
            idempotency_key=operation_idpk,
            created_at=now,
        ))
        session.add(InboundMessage(
            msg_id=str(uuid4()),
            idpk=envelope_idpk,
            message_type="swap-proposed",
            status="PROCESSED",
            sender="central",
            received_at=now,
            processed_at=now,
        ))
        execution, _ = get_or_create_saga_execution(
            session,
            idpk=operation_idpk,
            role="SELLER",
            timeout_at=now + timedelta(minutes=5),
            now=now,
        )
        proposal, _ = register_task_token(
            session,
            execution,
            task_token="proposal-token-e3-024",
            wait_type="PROPOSAL",
            correlation_idpk=operation_idpk,
            timeout_at=now + timedelta(minutes=2),
            now=now,
        )
        register_task_token(
            session,
            execution,
            task_token="terminal-token-e3-024",
            wait_type="TERMINAL",
            swap_id=swap_id,
            timeout_at=now + timedelta(minutes=4),
            now=now,
        )
        session.commit()

        callback = resolve_wallet_callback(
            session,
            envelope_idpk=envelope_idpk,
            event_type="swap-proposed",
            data={
                "idpk": operation_idpk,
                "swapId": swap_id,
                "expiresAt": (now + timedelta(minutes=1)).isoformat(),
            },
        )

        assert callback is not None
        assert callback["receipt"] == proposal.task_token_hash
        assert callback["output"]["outcome"] == "PROPOSED"
