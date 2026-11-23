"""E3-047: relay de callbacks después del commit económico (PostgreSQL real).

El conftest aísla sólo energyshark_test_*; no conecta a AWS ni a Wallet.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.database import engine
from app.main import app
from app.models import LedgerEntry, SagaExecution, SagaTaskToken, SwapRecord
from app.services.buyer_callback_dispatch import claim_buyer_callbacks, finish_buyer_callback
from app.services.saga_tokens import register_task_token
from app.services.wallet_service import WalletService
from tests.test_e3044_buyer_saga_db import _incoming, _post

ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:buyer-callback-test"


@pytest.fixture
def context(monkeypatch):
    monkeypatch.setenv("E3_BUYER_SAGA_ENABLED", "true")
    monkeypatch.setenv("E3_BUYER_STATE_MACHINE_ARN", ARN)
    cycle, _, _, message = _incoming()
    assert _post(message)["status"] == "ok"
    with Session(engine) as session:
        saga = session.exec(select(SagaExecution).where(SagaExecution.cycle_id == cycle)).one()
        swap = session.exec(select(SwapRecord).where(SwapRecord.cycle_id == cycle)).one()
        return {"cycle": cycle, "saga": saga.idpk, "swap": swap.swap_id,
                "record": swap.id, "idpk": swap.idpk}


def _register(ctx, *, timeout=60):
    with Session(engine) as session:
        saga = session.exec(select(SagaExecution).where(
            SagaExecution.idpk == ctx["saga"])).one()
        token, created = register_task_token(
            session, saga, task_token=f"test-token-{uuid4()}",
            wait_type="TERMINAL", swap_id=ctx["swap"],
            correlation_idpk=ctx["idpk"],
            timeout_at=datetime.now(timezone.utc) + timedelta(seconds=timeout),
        )
        assert created
        session.commit()
        return token.id


def _terminal(ctx, event_type="swap-settled"):
    with Session(engine) as session:
        record = WalletService(client=object()).process_swap_event(
            session, event_type=event_type,
            data={"swapId": ctx["swap"], "idpk": ctx["idpk"]},
        )
        assert record is not None
        session.commit()


def _claim(*, at=None):
    with Session(engine) as session:
        rows = claim_buyer_callbacks(session, now=at)
        session.commit()
        return rows


def _finish(item, outcome, *, at=None):
    with Session(engine) as session:
        result = finish_buyer_callback(
            session, token_id=item["id"], claim_token=item["claimToken"],
            outcome=outcome, now=at,
        )
        session.commit()
        return result


def test_callback_only_after_terminal_wallet_fact_and_commit(context):
    token_id = _register(context)
    assert _claim() == []
    _terminal(context)
    rows = _claim()
    assert len(rows) == 1
    assert rows[0]["id"] == token_id
    assert rows[0]["outcome"] == "SETTLED"
    assert rows[0]["swapId"] == context["swap"]
    assert rows[0]["taskToken"].startswith("test-token-")
    assert _finish(rows[0], "SUCCEEDED")
    assert _claim() == []
    with Session(engine) as session:
        assert session.get(SagaTaskToken, token_id).status == "SUCCEEDED"
        entries = session.exec(select(LedgerEntry).where(
            LedgerEntry.cycle_id == context["cycle"],
            LedgerEntry.operation_type == "SWAP_SETTLED",
        )).all()
        assert len(entries) == 1


def test_terminal_event_before_token_registration_is_not_lost(context):
    _terminal(context, "swap-rejected")
    token_id = _register(context)
    items = _claim()
    assert len(items) == 1 and items[0]["id"] == token_id
    assert items[0]["outcome"] == "REJECTED"


@pytest.mark.parametrize("event_type,outcome", [
    ("swap-rejected", "REJECTED"),
    ("swap-expired", "EXPIRED"),
    ("swap-cancelled", "CANCELLED"),
])
def test_all_wallet_terminal_events_reach_right_token(context, event_type, outcome):
    _register(context)
    _terminal(context, event_type)
    items = _claim()
    assert len(items) == 1 and items[0]["outcome"] == outcome


def test_duplicate_events_do_not_reopen_completed_callback_or_ledger(context):
    _register(context)
    _terminal(context)
    item = _claim()[0]
    assert _finish(item, "SUCCEEDED")
    _terminal(context)
    assert _claim() == []
    with Session(engine) as session:
        assert len(session.exec(select(LedgerEntry).where(
            LedgerEntry.cycle_id == context["cycle"],
            LedgerEntry.operation_type == "SWAP_SETTLED",
        )).all()) == 1


def test_lease_recovery_and_stale_result_fenced(context):
    _register(context)
    _terminal(context)
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        first = claim_buyer_callbacks(session, now=now, lease_seconds=5)[0]
        session.commit()
    assert _claim(at=now + timedelta(seconds=3)) == []
    recovered = _claim(at=now + timedelta(seconds=6))[0]
    assert recovered["id"] == first["id"]
    assert recovered["claimToken"] != first["claimToken"]
    assert not _finish(first, "SUCCEEDED", at=now + timedelta(seconds=7))
    assert _finish(recovered, "SUCCEEDED", at=now + timedelta(seconds=7))


def test_retry_backoff_keeps_waiting_with_same_task_token(context):
    _register(context)
    _terminal(context)
    now = datetime.now(timezone.utc)
    item = _claim(at=now)[0]
    assert _finish(item, "RETRY", at=now)
    assert _claim(at=now + timedelta(seconds=1)) == []
    retry = _claim(at=now + timedelta(seconds=3))[0]
    assert retry["taskToken"] == item["taskToken"]
    assert _finish(retry, "SUCCEEDED", at=now + timedelta(seconds=3))


def test_ambiguous_aws_result_is_not_automatic_success(context):
    token_id = _register(context)
    _terminal(context)
    item = _claim()[0]
    assert _finish(item, "AMBIGUOUS")
    assert _claim() == []
    with Session(engine) as session:
        assert session.get(SagaTaskToken, token_id).status == "AMBIGUOUS"


def test_wrong_swap_or_execution_does_not_claim(context):
    token_id = _register(context)
    _terminal(context)
    with Session(engine) as session:
        token = session.get(SagaTaskToken, token_id)
        token.swap_id = "wrong-swap-id"
        session.add(token)
        session.commit()
    assert _claim() == []


def test_expired_token_is_not_sent(context):
    token_id = _register(context, timeout=1)
    _terminal(context)
    assert _claim(at=datetime.now(timezone.utc) + timedelta(seconds=3)) == []


def test_concurrent_claims_are_disjoint(context):
    _register(context)
    _terminal(context)
    gate = Barrier(2)
    def worker():
        with Session(engine) as session:
            gate.wait(timeout=10)
            item = claim_buyer_callbacks(session, lease_seconds=15)
            session.commit()
            return [x["id"] for x in item]
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [f.result(timeout=15) for f in (executor.submit(worker), executor.submit(worker))]
    assert sorted(len(x) for x in results) == [0, 1]


def test_internal_http_claim_and_fenced_result(context):
    _register(context)
    _terminal(context)
    client = TestClient(app)
    claim = client.post("/internal/saga-callbacks/claims", json={"limit": 1})
    assert claim.status_code == 200, claim.text
    item = claim.json()["items"][0]
    bad = client.post(f"/internal/saga-callbacks/{item['id']}/result", json={
        "claimToken": str(uuid4()), "outcome": "SUCCEEDED"})
    assert bad.status_code == 409
    ok = client.post(f"/internal/saga-callbacks/{item['id']}/result", json={
        "claimToken": item["claimToken"], "outcome": "SUCCEEDED"})
    assert ok.status_code == 200
    assert client.post("/internal/saga-callbacks/claims", json={}).json()["items"] == []


def test_out_of_order_rejection_then_settlement_before_callback_prefers_wallet_fact(context):
    _register(context)
    _terminal(context, "swap-rejected")
    _terminal(context, "swap-settled")
    item = _claim()[0]
    assert item["outcome"] == "SETTLED"
    assert _finish(item, "SUCCEEDED")
    with Session(engine) as session:
        entries = session.exec(select(LedgerEntry).where(
            LedgerEntry.cycle_id == context["cycle"],
            LedgerEntry.operation_type == "SWAP_SETTLED",
        )).all()
        assert len(entries) == 1
