"""E2-022/E2-028: los fixtures usados en pruebas cumplen el contrato v3.0.1."""

import hashlib
import json

import pytest
from jsonschema import ValidationError

from tests.contracts_v3 import SCHEMAS, example, schema_hashes, validate


def test_vendored_schemas_match_official_hashes():
    for relative, expected in schema_hashes().items():
        digest = hashlib.sha256((SCHEMAS / relative).read_bytes()).hexdigest()
        assert digest == expected, f"schema modificado: {relative}"


def test_official_examples_are_schema_valid():
    validate("amqp/status-statement", example("status-statement.json"))
    validate("amqp/offer", example("offer.json"))
    validate("amqp/swap-settled", example("swap-settled.json"))
    validate("wallet/balance", example("wallet-balance-response.json"))


def test_status_statement_exposes_what_e2_028_needs():
    payload = example("status-statement.json")
    energy = payload["data"]["energy"]
    market = payload["data"]["market"]

    assert "sellable" in energy
    assert "buybackPrice" in market
    assert "minBid" in market
    assert "imbalancePrice" in market


def test_balances_after_uses_uppercase_and_rejects_lowercase():
    payload = example("swap-settled.json")
    balances = payload["data"]["balancesAfter"]

    assert set(balances) == {"CR", "KWH"}

    invalid = {
        "kwh": {"available": 1, "held": 0},
        "cr": {"available": 1, "held": 0},
        "creditLine": 0,
    }
    with pytest.raises(ValidationError):
        validate("common/balances-after", invalid)


def test_balances_after_rejects_credit_line():
    payload = example("swap-settled.json")
    balances = json.loads(json.dumps(payload["data"]["balancesAfter"]))
    balances["CR"]["creditLine"] = 100

    with pytest.raises(ValidationError):
        validate("common/balances-after", balances)


def test_balance_response_requires_close_fields():
    payload = example("wallet-balance-response.json")

    assert payload["lastSettledCycle"] == "cycle-9430"
    assert payload["balanceAtLastClose"]["CR"] == 1150000.0
