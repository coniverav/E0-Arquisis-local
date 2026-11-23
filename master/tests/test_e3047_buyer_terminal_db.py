"""E3-047: finalizar BUYER sólo tras evidencia Wallet persistida."""
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlmodel import Session, select

from app.config import CITY_ID
from app.database import engine
from app.models import LedgerEntry, SagaExecution, SwapRecord
from tests.test_e3047_buyer_callbacks_db import context, _terminal, _register

LAMBDA_DIR = Path(__file__).resolve().parents[2] / "infra" / "cdk" / "lambda_src" / "saga_task"
sys.path.insert(0, str(LAMBDA_DIR))
from postgres_store import PostgresStore  # noqa: E402
from buyer_terminal import finalize_buyer, BuyerTerminalInconsistent, BuyerTerminalPending  # noqa: E402


def _store():
    return PostgresStore(
        engine.url.render_as_string(hide_password=False).replace(
            "postgresql+psycopg://", "postgresql://", 1),
        city_id=CITY_ID,
    )


def _state(ctx):
    with Session(engine) as session:
        saga = session.exec(select(SagaExecution).where(SagaExecution.idpk == ctx["saga"])).one()
        swap = session.exec(select(SwapRecord).where(SwapRecord.idpk == ctx["idpk"])).one()
        count = len(session.exec(select(LedgerEntry).where(
            LedgerEntry.cycle_id == ctx["cycle"],
            LedgerEntry.operation_type == "SWAP_SETTLED",
        )).all())
        return saga.status, swap.status, count


def test_settled_requires_ledger_wallet_event_and_is_idempotent(context):
    with pytest.raises(BuyerTerminalPending):
        finalize_buyer(_store(), saga_execution_id=context["saga"],
                       result={"status": "SETTLED"})
    _terminal(context)
    first = finalize_buyer(_store(), saga_execution_id=context["saga"],
                           result={"status": "SETTLED"})
    second = finalize_buyer(_store(), saga_execution_id=context["saga"],
                            result={"status": "SETTLED"})
    assert first["alreadyFinalized"] is False and second["alreadyFinalized"] is True
    assert _state(context) == ("SETTLED", "SETTLED", 1)


@pytest.mark.parametrize("event", ["swap-rejected", "swap-expired", "swap-cancelled"])
def test_wallet_rejection_only_after_wallet_fact(context, event):
    _terminal(context, event)
    result = finalize_buyer(_store(), saga_execution_id=context["saga"],
                            result={"status": "REJECTED", "reason": "WALLET_REJECTED"})
    assert result["status"] == "REJECTED"
    assert _state(context)[0] == "REJECTED"


@pytest.mark.parametrize("reason", ["INVALID_SWAP_TOKEN", "DEADLINE_EXPIRED"])
def test_local_rejection_before_any_http_is_allowed(context, reason):
    result = finalize_buyer(_store(), saga_execution_id=context["saga"],
                            result={"status": "REJECTED", "reason": reason})
    assert result["status"] == "REJECTED"
    assert _state(context) == ("REJECTED", "REJECTED", 0)


def test_rejection_after_http_without_wallet_fact_is_not_confirmed(context):
    with Session(engine) as session:
        swap = session.exec(select(SwapRecord).where(SwapRecord.idpk == context["idpk"])).one()
        swap.accept_attempts = 1
        swap.status = "ACCEPT_SUBMITTED"
        session.commit()
    with pytest.raises(BuyerTerminalPending):
        finalize_buyer(_store(), saga_execution_id=context["saga"],
                       result={"status": "REJECTED", "reason": "WALLET_REJECTED"})
    assert _state(context)[0] == "PENDING_START"


def test_timeout_does_not_infer_wallet_settlement(context):
    result = finalize_buyer(_store(), saga_execution_id=context["saga"],
                            result={"status": "TIMED_OUT", "reason": "SWAP_STILL_PROPOSED"})
    assert result["status"] == "TIMED_OUT"
    assert _state(context) == ("TIMED_OUT", "SUBMITTED", 0)


def test_timeout_cannot_replace_wallet_settlement(context):
    _terminal(context)
    with pytest.raises(BuyerTerminalInconsistent):
        finalize_buyer(_store(), saga_execution_id=context["saga"],
                       result={"status": "TIMED_OUT"})
    assert _state(context)[2] == 1


def test_finalization_never_rewrites_terminal_outcome(context):
    _terminal(context)
    finalize_buyer(_store(), saga_execution_id=context["saga"],
                   result={"status": "SETTLED"})
    with pytest.raises(BuyerTerminalInconsistent):
        finalize_buyer(_store(), saga_execution_id=context["saga"],
                       result={"status": "REJECTED", "reason": "WALLET_REJECTED"})
