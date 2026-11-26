import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pamqp.commands import Basic

from outbox_relay import (
    SagaIdentityMismatch,
    broker_relay_enabled,
    publish_broker_item,
    saga_relay_enabled,
    start_execution_once,
)


MACHINE = "arn:aws:states:us-east-1:123456789012:stateMachine:energyshark-seller"


def broker_item():
    idpk, msg_id = str(uuid4()), str(uuid4())
    payload = {
        "idpk": idpk,
        "msgId": msg_id,
        "type": "offer",
        "timestamp": "2026-11-21T12:00:00Z",
        "cityId": "KLD",
        "cycleId": "cycle-relay",
        "data": {
            "offerId": "KLD-cycle-relay",
            "quantity": 20,
            "pricePerEnergy": 3.5,
        },
    }
    return {
        "id": 1,
        "idpk": idpk,
        "msgId": msg_id,
        "type": "offer",
        "payload": payload,
        "exchange": "market",
        "routingKey": "market.offer.KLD",
    }


def saga_item():
    idpk = str(uuid4())
    value = {"idpk": idpk, "role": "SELLER", "cycleId": "cycle-relay"}
    return {
        "id": 2,
        "idpk": idpk,
        "stateMachineArn": MACHINE,
        "executionName": idpk,
        "executionInput": json.dumps(value, sort_keys=True, separators=(",", ":")),
    }


def test_broker_publish_requires_positive_publisher_confirm():
    item = broker_item()
    directed, market = AsyncMock(), AsyncMock()
    market.publish.return_value = Basic.Ack(delivery_tag=1)
    asyncio.run(publish_broker_item(
        item,
        directed_exchange=directed,
        market_exchange=market,
        city_id="KLD",
        directed_exchange_name="energy.x",
        market_exchange_name="market",
        central_routing_key="central",
    ))
    market.publish.assert_awaited_once()
    directed.publish.assert_not_awaited()
    sent = market.publish.await_args.args[0]
    assert json.loads(sent.body) == item["payload"]
    assert sent.user_id == "city.KLD"


@pytest.mark.parametrize("confirmation", [Basic.Nack(delivery_tag=1), None])
def test_broker_nack_or_missing_confirm_is_not_success(confirmation):
    market = AsyncMock()
    market.publish.return_value = confirmation
    with pytest.raises(RuntimeError, match="NOT_CONFIRMED"):
        asyncio.run(publish_broker_item(
            broker_item(),
            directed_exchange=AsyncMock(),
            market_exchange=market,
            city_id="KLD",
            directed_exchange_name="energy.x",
            market_exchange_name="market",
            central_routing_key="central",
        ))


def test_start_execution_uses_stored_name_and_input_without_rewriting():
    item = saga_item()
    execution_arn = MACHINE.replace(":stateMachine:", ":execution:") + f":{item['idpk']}"
    client = SimpleNamespace(
        start_execution=lambda **kwargs: {"executionArn": execution_arn},
    )
    assert start_execution_once(item, stepfunctions=client) == execution_arn


class AlreadyExists(Exception):
    response = {"Error": {"Code": "ExecutionAlreadyExists"}}


def test_existing_execution_is_correlated_only_when_identity_matches():
    item = saga_item()
    expected = MACHINE.replace(":stateMachine:", ":execution:") + f":{item['idpk']}"

    class Client:
        def start_execution(self, **kwargs):
            assert kwargs == {
                "stateMachineArn": MACHINE,
                "name": item["idpk"],
                "input": item["executionInput"],
            }
            raise AlreadyExists()

        def describe_execution(self, **kwargs):
            assert kwargs == {"executionArn": expected}
            return {
                "stateMachineArn": MACHINE,
                "name": item["idpk"],
                "input": item["executionInput"],
            }

    assert start_execution_once(item, stepfunctions=Client()) == expected


def test_existing_execution_with_other_input_is_never_adopted():
    item = saga_item()

    class Client:
        def start_execution(self, **kwargs):
            raise AlreadyExists()

        def describe_execution(self, **kwargs):
            return {
                "stateMachineArn": MACHINE,
                "name": item["idpk"],
                "input": '{"idpk":"other"}',
            }

    with pytest.raises(SagaIdentityMismatch):
        start_execution_once(item, stepfunctions=Client())


def test_relays_remain_disabled_by_default(monkeypatch):
    monkeypatch.delenv("E3_BROKER_OUTBOX_RELAY_ENABLED", raising=False)
    monkeypatch.delenv("E3_SAGA_OUTBOX_RELAY_ENABLED", raising=False)
    assert broker_relay_enabled() is False
    assert saga_relay_enabled() is False


@pytest.mark.parametrize('field', ['expiresAt', 'leaseUntil'])
def test_expired_batch_item_is_never_published(field):
    item = broker_item()
    item[field] = '2000-01-01T00:00:00Z'
    directed, market = AsyncMock(), AsyncMock()
    with pytest.raises(ValueError, match='DEADLINE_EXPIRED'):
        asyncio.run(publish_broker_item(
            item, directed_exchange=directed, market_exchange=market,
            city_id='KLD', directed_exchange_name='energy.x',
            market_exchange_name='market', central_routing_key='central',
        ))
    directed.publish.assert_not_awaited()
    market.publish.assert_not_awaited()
