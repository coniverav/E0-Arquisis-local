"""E3-044: ingreso real, outbox Standard, rollback y exclusión del worker E2.

Usar sólo DATABASE_URL apuntando a energyshark_test_* (conftest existente).
Sin StartExecution real ni HTTP Wallet.
"""
import asyncio
import json
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.database import engine
from app.main import app
from app.models import MarketBid, OutboundMessage, SagaExecution, SwapRecord
from app.services.bid_responses import process_bid_reply
from app.services.message_audit import INBOUND_PROCESSED
from app.services.swap_accept import accept_pending_swaps, _accept_work
from tests.test_swap_accept_e2041 import _setup_bid, _token, _acceptance, FakeWallet

ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:e3044-buyer"


@pytest.fixture
def buyer_enabled(monkeypatch):
    monkeypatch.setenv("E3_BUYER_SAGA_ENABLED", "true")
    monkeypatch.setenv("E3_BUYER_STATE_MACHINE_ARN", ARN)


def _incoming(*, acceptance_idpk=None):
    cycle, offer, bid = _setup_bid()
    message = _acceptance(
        cycle, offer, bid,
        token=_token(swap_id="swp-77", proposer="HGW", memo=offer),
    )
    if acceptance_idpk is not None:
        message["idpk"] = acceptance_idpk
    return cycle, offer, bid, message


def _snapshot(cycle):
    with Session(engine) as session:
        return dict(
            bids=session.exec(select(MarketBid).where(MarketBid.cycle_id == cycle)).all(),
            records=session.exec(select(SwapRecord).where(SwapRecord.cycle_id == cycle)).all(),
            sagas=session.exec(select(SagaExecution).where(SagaExecution.cycle_id == cycle)).all(),
            starts=session.exec(select(OutboundMessage).where(
                OutboundMessage.cycle_id == cycle,
                OutboundMessage.message_type == "SAGA_START",
            )).all(),
        )


def _post(message):
    response = TestClient(app).post("/internal/messages", json=message)
    assert response.status_code == 200, response.text
    return response.json()


def test_ingress_creates_one_buyer_saga_and_start_atomically(buyer_enabled):
    cycle, offer, bid, message = _incoming()
    assert _post(message)["status"] == "ok"
    state = _snapshot(cycle)
    assert len(state["records"]) == len(state["sagas"]) == len(state["starts"]) == 1
    assert state["bids"][0].status == "ACCEPTED"
    swap, saga, start = state["records"][0], state["sagas"][0], state["starts"][0]
    assert swap.role == "ACCEPTOR" and swap.status == "SUBMITTED"
    assert swap.source_bid_idpk == bid["idpk"]
    assert swap.idpk == swap.accept_idempotency_key == message["idpk"]
    assert swap.swap_token == message["data"]["swapToken"]
    assert saga.idpk == message["idpk"] and saga.role == "BUYER"
    assert saga.swap_id == swap.swap_id == message["data"]["swapId"]
    assert start.delivery_kind == "STEP_FUNCTIONS" and start.status == "PENDING"
    assert start.state_machine_arn == ARN
    assert start.idpk == start.execution_name == message["idpk"]
    assert start.dispatch_required is True and start.expires_at is None
    data = json.loads(start.execution_input)
    assert data["idpk"] == message["idpk"]
    assert data["swapRecordId"] == swap.id
    assert data["bid"]["idpk"] == bid["idpk"]
    assert data["bid"]["msgId"] == message["data"]["target"]
    assert data["swap"]["swapId"] == message["data"]["swapId"]
    assert data["swap"]["acceptIdempotencyKey"] == swap.accept_idempotency_key
    assert message["data"]["swapToken"] not in start.execution_input
    assert "swapToken" not in start.execution_input


def test_worker_e2_does_not_accept_a_buyer_saga(buyer_enabled):
    cycle, offer, bid, message = _incoming()
    _post(message)
    wallet = FakeWallet()
    with Session(engine) as session:
        stats = asyncio.run(accept_pending_swaps(session, wallet_service=wallet))
        assert _accept_work(session, 10, datetime.now(timezone.utc)) == []
    assert stats["scanned"] == 0 and stats["accepted"] == 0
    assert wallet.calls == []
    assert _snapshot(cycle)["records"][0].status == "SUBMITTED"


def test_exact_redelivery_does_not_duplicate_start(buyer_enabled):
    cycle, offer, bid, message = _incoming()
    assert _post(message)["status"] == "ok"
    assert _post(message)["status"] == "duplicate"
    replay = deepcopy(message)
    replay["idpk"] = str(uuid4())
    replay["msgId"] = str(uuid4())
    assert _post(replay)["status"] == "not_processed"
    state = _snapshot(cycle)
    assert len(state["records"]) == len(state["sagas"]) == len(state["starts"]) == 1


def test_flag_disabled_preserves_e2_ingress(monkeypatch):
    monkeypatch.delenv("E3_BUYER_SAGA_ENABLED", raising=False)
    cycle, offer, bid, message = _incoming()
    assert _post(message)["status"] == "ok"
    state = _snapshot(cycle)
    assert state["bids"][0].status == "ACCEPTED"
    assert state["records"] == state["sagas"] == state["starts"] == []


def test_missing_arn_rolls_back_bid_and_start(monkeypatch):
    monkeypatch.setenv("E3_BUYER_SAGA_ENABLED", "true")
    monkeypatch.delenv("E3_BUYER_STATE_MACHINE_ARN", raising=False)
    cycle, offer, bid, message = _incoming()
    # La función participa de la transacción del ingress: el llamador revierte.
    from app.schemas import ProtocolMessageIn
    envelope = ProtocolMessageIn.model_validate(message)
    with Session(engine) as session:
        with pytest.raises(ValueError, match="E3_BUYER_STATE_MACHINE_ARN_REQUIRED"):
            process_bid_reply(session, payload=envelope, now=datetime.now(timezone.utc))
        session.rollback()
    state = _snapshot(cycle)
    assert state["bids"][0].status == "PENDING"
    assert state["records"] == state["sagas"] == state["starts"] == []


def test_direct_rollback_does_not_leave_saga_or_swap(buyer_enabled):
    cycle, offer, bid, message = _incoming()
    from app.schemas import ProtocolMessageIn
    envelope = ProtocolMessageIn.model_validate(message)
    with Session(engine) as session:
        status, error = process_bid_reply(session, payload=envelope,
                                          now=datetime.now(timezone.utc))
        assert (status, error) == (INBOUND_PROCESSED, None)
        assert session.exec(select(SwapRecord)).one().source_bid_idpk == bid["idpk"]
        session.rollback()
    state = _snapshot(cycle)
    assert state["bids"][0].status == "PENDING"
    assert state["records"] == state["sagas"] == state["starts"] == []


def test_duplicate_target_under_new_acceptance_idpk_is_not_processed(buyer_enabled):
    cycle, offer, bid, message = _incoming()
    _post(message)
    other = deepcopy(message)
    other["idpk"] = str(uuid4())
    other["msgId"] = str(uuid4())
    assert _post(other)["status"] == "not_processed"
    assert len(_snapshot(cycle)["starts"]) == 1
