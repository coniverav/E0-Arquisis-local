"""Pruebas sin PostgreSQL de contrato, validación y serialización E3-025."""
import json
from uuid import uuid4
import pytest

from app.services.outbox_contract import (
    canonical_execution_input, validate_broker_envelope, validate_execution_identity,
)


def example():
    return {
        "msgId": str(uuid4()), "idpk": str(uuid4()), "type": "offer",
        "cycleId": "cycle-43", "cityId": "KLD", "timestamp": "2026-11-21T12:00:00Z",
        "data": {"offerId": "KLD-cycle-43", "quantity": 100},
    }


def test_envelope_is_deepcopied_and_ids_are_not_generated_again():
    original = example()
    result = validate_broker_envelope(original)
    assert result == original
    assert result is not original
    original["data"]["quantity"] = 400
    assert result["data"]["quantity"] == 100
    assert result["msgId"] != result["idpk"]


@pytest.mark.parametrize("key", ["msgId", "idpk", "type", "cycleId"])
def test_required_envelope_fields(key):
    bad = example()
    bad.pop(key)
    with pytest.raises(ValueError, match="ENVELOPE_MISSING"):
        validate_broker_envelope(bad)


def test_non_object_envelope():
    with pytest.raises(ValueError, match="ENVELOPE_NOT_OBJECT"):
        validate_broker_envelope([1, 2])


def test_invalid_nan_and_non_serializable_envelope():
    bad = example()
    bad["data"]["price"] = float("nan")
    with pytest.raises(ValueError):
        validate_broker_envelope(bad)
    bad["data"]["price"] = {1, 2}
    with pytest.raises(TypeError):
        validate_broker_envelope(bad)


def test_execution_input_canonical_for_mapping():
    raw = canonical_execution_input({"b": 4, "a": {"é": "sí"}})
    assert raw == '{"a":{"é":"sí"},"b":4}'
    assert json.loads(raw) == {"b": 4, "a": {"é": "sí"}}


def test_string_execution_input_is_byte_stable():
    raw = '{ "role": "SELLER", "idpk": "abc" }'
    assert canonical_execution_input(raw) == raw


@pytest.mark.parametrize("value", ["[]", "null", "17", "\"test\""])
def test_execution_input_must_be_json_object(value):
    with pytest.raises(ValueError, match="EXECUTION_INPUT_NOT_OBJECT"):
        canonical_execution_input(value)


@pytest.mark.parametrize("name", ["aaa.bbb", "hello world", "", "x" * 81])
def test_execution_identity_rejects_invalid_name(name):
    with pytest.raises(ValueError, match="EXECUTION_NAME_MUST_EQUAL_IDPK"):
        validate_execution_identity(name)


def test_execution_name_exactly_matches_idpk():
    token = str(uuid4())
    assert validate_execution_identity(token) == token
    with pytest.raises(ValueError, match="EXECUTION_NAME_MUST_EQUAL_IDPK"):
        validate_execution_identity(token, str(uuid4()))
