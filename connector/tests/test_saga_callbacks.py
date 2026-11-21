"""E3-024: entrega recuperable de callbacks Step Functions."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from saga_callbacks import (
    dispatch_stepfunctions_callback,
    handle_wallet_saga_callback,
)


RECEIPT = "a" * 64
PROTOCOL = {"idpk": "event-idpk", "type": "swap-settled"}
SUCCESS = {
    "mode": "SUCCESS",
    "taskToken": "secret-task-token",
    "receipt": RECEIPT,
    "output": {"outcome": "SETTLED", "swapId": "swap-24"},
}


class AwsError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeStepFunctions:
    def __init__(self, error: str | None = None):
        self.error = error
        self.successes = []
        self.failures = []

    def send_task_success(self, **kwargs):
        if self.error:
            raise AwsError(self.error)
        self.successes.append(kwargs)

    def send_task_failure(self, **kwargs):
        if self.error:
            raise AwsError(self.error)
        self.failures.append(kwargs)


def test_send_task_success_uses_exact_token_and_safe_output():
    stepfunctions = FakeStepFunctions()
    result = asyncio.run(dispatch_stepfunctions_callback(
        SUCCESS,
        stepfunctions=stepfunctions,
    ))

    assert result == "SUCCEEDED"
    assert stepfunctions.successes == [{
        "taskToken": "secret-task-token",
        "output": json.dumps(SUCCESS["output"], separators=(",", ":")),
    }]
    assert stepfunctions.failures == []


def test_send_task_failure_is_available_for_technical_failures():
    stepfunctions = FakeStepFunctions()
    result = asyncio.run(dispatch_stepfunctions_callback({
        "mode": "FAILURE",
        "taskToken": "secret-task-token",
        "error": "WalletCallbackInvalid",
        "cause": "Evento no procesable",
    }, stepfunctions=stepfunctions))

    assert result == "FAILED"
    assert stepfunctions.failures == [{
        "taskToken": "secret-task-token",
        "error": "WalletCallbackInvalid",
        "cause": "Evento no procesable",
    }]


def test_transient_error_retries_and_closed_task_is_idempotent():
    transient = asyncio.run(dispatch_stepfunctions_callback(
        SUCCESS,
        stepfunctions=FakeStepFunctions("ThrottlingException"),
    ))
    already_delivered = asyncio.run(dispatch_stepfunctions_callback(
        SUCCESS,
        stepfunctions=FakeStepFunctions("TaskDoesNotExist"),
    ))
    timed_out = asyncio.run(dispatch_stepfunctions_callback(
        SUCCESS,
        stepfunctions=FakeStepFunctions("TaskTimedOut"),
    ))

    assert transient == "RETRY"
    # El token proviene de nuestra persistencia durable. Si AWS ya no lo
    # encuentra tras una reentrega, el callback pudo completarse antes de que
    # Master registrara el resultado; no se intenta otra operación económica.
    assert already_delivered == "SUCCEEDED"
    assert timed_out == "FAILED"


def test_transient_callback_does_not_mark_master_and_requests_redelivery(monkeypatch):
    monkeypatch.setenv("E3_SAGA_CALLBACKS_ENABLED", "true")
    client = AsyncMock()
    result = asyncio.run(handle_wallet_saga_callback(
        client,
        protocol_payload=PROTOCOL,
        master_payload={"sagaCallback": SUCCESS},
        result_url="http://master/internal/saga-callbacks/result",
        stepfunctions=FakeStepFunctions("ServiceUnavailable"),
    ))

    assert result == "retry"
    client.post.assert_not_awaited()


def test_success_is_persisted_before_rabbit_delivery_can_be_acked(monkeypatch):
    monkeypatch.setenv("E3_SAGA_CALLBACKS_ENABLED", "true")
    client = AsyncMock()
    client.post.return_value = SimpleNamespace(status_code=200)
    stepfunctions = FakeStepFunctions()

    result = asyncio.run(handle_wallet_saga_callback(
        client,
        protocol_payload=PROTOCOL,
        master_payload={"sagaCallback": SUCCESS},
        result_url="http://master/internal/saga-callbacks/result",
        stepfunctions=stepfunctions,
    ))

    assert result == "ack"
    client.post.assert_awaited_once_with(
        "http://master/internal/saga-callbacks/result",
        json={
            "receipt": RECEIPT,
            "status": "SUCCEEDED",
            "callbackIdpk": "event-idpk",
        },
    )


def test_master_result_failure_retries_without_a_second_wallet_effect(monkeypatch):
    monkeypatch.setenv("E3_SAGA_CALLBACKS_ENABLED", "true")
    client = AsyncMock()
    client.post.return_value = SimpleNamespace(status_code=503)

    result = asyncio.run(handle_wallet_saga_callback(
        client,
        protocol_payload=PROTOCOL,
        master_payload={"sagaCallback": SUCCESS},
        result_url="http://master/internal/saga-callbacks/result",
        stepfunctions=FakeStepFunctions(),
    ))

    assert result == "retry"
    client.post.assert_awaited_once()


def test_feature_flag_keeps_existing_connector_path_unchanged(monkeypatch):
    monkeypatch.setenv("E3_SAGA_CALLBACKS_ENABLED", "false")
    client = AsyncMock()
    stepfunctions = FakeStepFunctions()

    result = asyncio.run(handle_wallet_saga_callback(
        client,
        protocol_payload=PROTOCOL,
        master_payload={"sagaCallback": SUCCESS},
        result_url="http://master/internal/saga-callbacks/result",
        stepfunctions=stepfunctions,
    ))

    assert result == "ack"
    assert stepfunctions.successes == []
    client.post.assert_not_awaited()
