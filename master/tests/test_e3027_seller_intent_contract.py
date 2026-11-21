"""Contrato puro para E3-027: no requiere AWS, RabbitMQ ni Postgres."""
from datetime import datetime, timezone
from decimal import Decimal
from copy import deepcopy
import json

import pytest

from app.services.seller_saga_intent import build_seller_execution_input
from app.services.outbox_contract import canonical_execution_input


def values(**overrides):
    base = dict(
        bid_idpk="820e9739-a3e6-4a6d-b4f4-7193a47d34ab",
        bid_msg_id="dd282474-5818-4317-ab1c-e70d5e396ea6",
        cycle_id="cycle-42",
        offer_id="KLD-cycle-42",
        buyer_city_id="TAL",
        quantity_kwh=80,
        give_amount=Decimal("80"),
        take_amount=Decimal("340.00"),
        wallet_idempotency_key="820e9739-a3e6-4a6d-b4f4-7193a47d34ab",
        market_closes_at=datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return base


def test_identity_role_and_economic_values():
    payload = build_seller_execution_input(**values())
    assert payload["schemaVersion"] == 1
    assert payload["idpk"] == payload["bid"]["idpk"] == payload["swap"]["idempotencyKey"]
    assert payload["role"] == "SELLER"
    assert payload["cycleId"] == "cycle-42"
    assert payload["bid"]["quantityKwh"] == 80
    assert payload["swap"]["give"] == {"asset": "KWH", "amount": "80"}
    assert payload["swap"]["take"] == {"asset": "CR", "amount": "340.00"}
    assert payload["marketClosesAt"] == "2026-11-23T15:00:00Z"
    assert "apiKey" not in json.dumps(payload).lower()
    assert "swapToken" not in json.dumps(payload)


def test_serialized_input_is_deterministic_and_valid():
    first = build_seller_execution_input(**values())
    second = build_seller_execution_input(**values())
    assert first == second
    assert canonical_execution_input(first) == canonical_execution_input(second)
    assert json.loads(canonical_execution_input(first)) == first


@pytest.mark.parametrize("change", [
    {"bid_idpk": ""},
    {"buyer_city_id": ""},
    {"quantity_kwh": 0},
    {"quantity_kwh": -2},
    {"quantity_kwh": True},
    {"quantity_kwh": 2.5},
    {"give_amount": Decimal("79")},
    {"give_amount": Decimal("NaN")},
    {"take_amount": Decimal("NaN")},
    {"take_amount": Decimal("-1.00")},
    {"take_amount": Decimal("1.001")},
    {"wallet_idempotency_key": "some-other-uuid"},
    {"market_closes_at": datetime(2026, 11, 23, 15)},
])
def test_rejects_invalid_saga_contract(change):
    with pytest.raises(ValueError):
        build_seller_execution_input(**values(**change))


def test_source_payload_is_not_shared_mutable_global():
    a = build_seller_execution_input(**values())
    b = build_seller_execution_input(**values())
    snapshot = deepcopy(b)
    a["swap"]["give"]["amount"] = "900"
    assert b == snapshot


def test_market_closes_at_uses_utc():
    from datetime import timedelta
    payload = build_seller_execution_input(**values(
        market_closes_at=datetime(2026, 11, 23, 12, tzinfo=timezone(timedelta(hours=-3)))
    ))
    assert payload["marketClosesAt"] == "2026-11-23T15:00:00Z"
