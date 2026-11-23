"""E3-047: callback AWS sólo con resultado DB, sin interpretar TaskDoesNotExist como OK."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import asyncio

import pytest

from outbox_relay import (
    send_buyer_callback_once, claim_buyer_callback_items, finish_buyer_callback_item,
)


def _item():
    return {"id": 3, "claimToken": "lease-1", "taskToken": "opaque-task-token",
            "swapId": "swap-1", "sagaExecutionId": "acceptance-idpk", "outcome": "SETTLED"}


def test_aws_send_uses_stored_task_token_and_terminal_outcome():
    calls = []
    sf = SimpleNamespace(send_task_success=lambda **kw: calls.append(kw))
    send_buyer_callback_once(_item(), stepfunctions=sf)
    assert len(calls) == 1
    assert calls[0]["taskToken"] == "opaque-task-token"
    assert json.loads(calls[0]["output"]) == {"outcome": "SETTLED", "swapId": "swap-1"}
    assert "sagaExecutionId" not in calls[0]["output"]


@pytest.mark.parametrize("outcome", ["REJECTED", "EXPIRED", "CANCELLED"])
def test_terminal_outcome_follows_wallet_fact(outcome):
    item = {**_item(), "outcome": outcome}
    seen = []
    send_buyer_callback_once(item, stepfunctions=SimpleNamespace(
        send_task_success=lambda **kw: seen.append(json.loads(kw["output"]))
    ))
    assert seen[0]["outcome"] == outcome


@pytest.mark.parametrize("field", ["taskToken", "swapId", "claimToken", "outcome"])
def test_bad_claim_rejected_before_aws(field):
    item = _item()
    item[field] = "" if field != "outcome" else "PROPOSED"
    with pytest.raises(ValueError, match="INVALID_BUYER_CALLBACK_ITEM"):
        send_buyer_callback_once(item, stepfunctions=SimpleNamespace(
            send_task_success=lambda **kw: pytest.fail("AWS no debe recibir esta solicitud")
        ))


def test_claim_and_result_endpoint_never_log_token():
    claim_response = SimpleNamespace(
        json=lambda: {"items": [_item()]},
        raise_for_status=lambda: None,
    )
    result_response = SimpleNamespace(status_code=200, raise_for_status=lambda: None)
    client = SimpleNamespace(post=AsyncMock(side_effect=[claim_response, result_response]))
    result = asyncio.run(claim_buyer_callback_items(client))
    assert result == [_item()]
    ok = asyncio.run(finish_buyer_callback_item(client, _item(), outcome="SUCCEEDED"))
    assert ok is True
    sent = client.post.await_args_list[-1].kwargs["json"]
    assert sent == {"claimToken": "lease-1", "outcome": "SUCCEEDED", "retrySeconds": 2}
    assert "taskToken" not in sent
