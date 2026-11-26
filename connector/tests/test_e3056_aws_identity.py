"""Identidad del destino, denegaciones IAM y credenciales renovables, sin red."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import boto3
import botocore.session
from botocore.credentials import RefreshableCredentials
from botocore.exceptions import ClientError
import pytest

import outbox_relay as relay

SELLER = "arn:aws:states:us-east-1:123456789012:stateMachine:seller:live"


@pytest.fixture
def intent(monkeypatch):
    monkeypatch.setenv("E3_SELLER_STATE_MACHINE_ARN", SELLER)
    return dict(stateMachineArn=SELLER, executionName="op-1", idpk="op-1",
                executionInput=json.dumps(dict(idpk="op-1", role="SELLER")))


@pytest.mark.parametrize("destination", [
    SELLER.replace("123456789012", "999999999999"),
    SELLER.replace("seller", "buyer"), SELLER.removesuffix(":live"),
    SELLER.replace("us-east-1", "us-west-2"), "*",
])
def test_unapproved_destination_never_reaches_aws(intent, destination):
    intent["stateMachineArn"] = destination
    client = Mock()
    with pytest.raises((ValueError, relay.SagaIdentityMismatch)):
        relay.start_execution_once(intent, stepfunctions=client)
    client.start_execution.assert_not_called()


def test_unconfigured_destination_is_rejected(intent, monkeypatch):
    monkeypatch.delenv("E3_SELLER_STATE_MACHINE_ARN")
    with pytest.raises(relay.SagaIdentityMismatch):
        relay.start_execution_once(intent, stepfunctions=Mock())


def test_response_arn_must_match_requested_identity(intent):
    client = Mock()
    client.start_execution.return_value = {"executionArn": "arn:aws:states:elsewhere"}
    with pytest.raises(relay.SagaIdentityMismatch):
        relay.start_execution_once(intent, stepfunctions=client)


def test_access_denied_propagates_without_describe(intent):
    client = Mock()
    client.start_execution.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException"}}, "StartExecution",
    )
    with pytest.raises(ClientError):
        relay.start_execution_once(intent, stepfunctions=client)
    client.describe_execution.assert_not_called()


def test_callback_denial_propagates():
    client = Mock()
    client.send_task_success.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException"}}, "SendTaskSuccess",
    )
    with pytest.raises(ClientError):
        relay.send_buyer_callback_once(
            dict(id=1, sagaExecutionId="op-1", taskToken="opaque", claimToken="claim",
                 swapId="swap-1", outcome="SETTLED"), stepfunctions=client,
        )


@pytest.mark.parametrize("url", [
    "https://other.example/internal/claims",
    "http://user:password@master:8000/internal/claims",
    "http://master:8001/internal/claims",
])
def test_untrusted_master_origin_is_rejected(monkeypatch, url):
    monkeypatch.setenv("MASTER_URL", "http://master:8000/messages")
    with pytest.raises(ValueError, match="UNTRUSTED_MASTER_ORIGIN"):
        relay._validate_master_origin(url)


def test_client_preserves_renewable_credentials(monkeypatch):
    def credentials(key, minutes):
        return dict(access_key=key, secret_key="test-secret", token="test-session",
                    expiry_time=(datetime.now(timezone.utc)
                                 + timedelta(minutes=minutes)).isoformat())
    refresh = Mock(return_value=credentials("renewed", 60))
    renewable = RefreshableCredentials.create_from_metadata(
        metadata=credentials("expired", -1), refresh_using=refresh,
        method="iam-role",
    )
    session = botocore.session.get_session()
    session._credentials = renewable
    sdk = boto3.Session(botocore_session=session, region_name="us-east-1")
    monkeypatch.setattr(boto3, "client", sdk.client)
    client = relay._stepfunctions_client()
    assert client._request_signer._credentials is renewable
    assert renewable.get_frozen_credentials().access_key == "renewed"
    refresh.assert_called_once()
    assert client.meta.config.connect_timeout == 3


def test_replay_description_denial_is_not_success(intent):
    client = Mock()
    client.start_execution.side_effect = ClientError(
        {"Error": {"Code": "ExecutionAlreadyExists"}}, "StartExecution",
    )
    client.describe_execution.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException"}}, "DescribeExecution",
    )
    with pytest.raises(ClientError):
        relay.start_execution_once(intent, stepfunctions=client)


def test_alias_replay_uses_unqualified_execution_arn(intent):
    candidate = SELLER.removesuffix(":live").replace(":stateMachine:", ":execution:") + ":op-1"
    client = Mock()
    client.start_execution.side_effect = ClientError(
        {"Error": {"Code": "ExecutionAlreadyExists"}}, "StartExecution",
    )
    client.describe_execution.return_value = dict(
        stateMachineArn=SELLER.removesuffix(":live"), name="op-1",
        input=intent["executionInput"],
    )
    assert relay.start_execution_once(intent, stepfunctions=client) == candidate
    client.describe_execution.assert_called_once_with(executionArn=candidate)


def test_callback_claim_origin_is_checked_before_http(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock
    monkeypatch.setenv("MASTER_URL", "http://master:8000/messages")
    monkeypatch.setattr(relay, "CALLBACK_CLAIM_URL", "https://other.example/claims")
    client = SimpleNamespace(post=AsyncMock())
    with pytest.raises(ValueError, match="UNTRUSTED_MASTER_ORIGIN"):
        asyncio.run(relay.claim_buyer_callback_items(client))
    client.post.assert_not_awaited()


def test_denied_callback_is_recorded_as_retry(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock
    item = dict(id=1, sagaExecutionId="op-1", taskToken="opaque", claimToken="claim",
                swapId="swap-1", outcome="SETTLED")
    client = Mock()
    client.send_task_success.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException"}}, "SendTaskSuccess",
    )
    monkeypatch.setattr(relay, "_stepfunctions_client", lambda: client)
    # El despacho directo se verifica arriba; aquí se prueba el resultado durable
    # del loop ante una denegación del worker.
    monkeypatch.setattr(
        relay.asyncio, "to_thread",
        AsyncMock(side_effect=ClientError(
            {"Error": {"Code": "AccessDeniedException"}}, "SendTaskSuccess",
        )),
    )
    monkeypatch.setattr(relay, "claim_buyer_callback_items", AsyncMock(return_value=[item]))
    finish = AsyncMock(return_value=True)
    monkeypatch.setattr(relay, "finish_buyer_callback_item", finish)
    monkeypatch.setattr(relay.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(relay.buyer_callback_relay_forever(http_timeout=1, reconnect_seconds=1))
    finish.assert_awaited_once()
    assert finish.await_args.kwargs["outcome"] == "RETRY"
