"""E2-076: los fixtures emitidos en pruebas cumplen el contrato v3.0.2."""

import hashlib
import json

import pytest
from jsonschema import ValidationError

from tests.contracts_v3 import SCHEMAS, example, schema_hashes, validate


def test_vendored_schemas_match_official_hashes():
    actual = {
        path.relative_to(SCHEMAS).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in SCHEMAS.rglob("*.schema.json")
    }
    assert actual == schema_hashes(), "subconjunto de schemas distinto del tag fijado"


def test_synthetic_examples_are_schema_valid():
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


def test_swap_token_claims_example_is_valid():
    payload = example("swap-token-claims.json")
    validate("token/swap-token-claims", payload)

    assert payload["iss"] == "wallet.energyshark"
    assert set(payload) == {
        "iss", "sub", "iat", "exp", "proposer", "give", "take", "memo",
    }
    assert payload["give"]["asset"] in {"CR", "KWH"}
    assert payload["take"]["asset"] in {"CR", "KWH"}


def test_trade_recorded_example_is_valid():
    payload = example("trade-recorded.json")
    validate("amqp/trade-recorded", payload)

    assert payload["type"] == "trade-recorded"
    assert payload["data"]["swapId"]
    assert payload["data"]["quantity"] > 0
    assert payload["data"]["transportTotal"] >= 0


def test_imbalance_settlement_example_is_valid():
    payload = example("imbalance-settlement.json")
    validate("amqp/imbalance-settlement", payload)

    assert payload["type"] == "imbalance-settlement"
    assert payload["data"]["coverage"] == (
        payload["data"]["kwhHeld"] - payload["data"]["deficit"]
    )


def test_statement_page_example_is_valid():
    payload = example("wallet-statement-response.json")
    validate("wallet/statement-page", payload)

    assert "entries" in payload and "nextCursor" in payload
    for entry in payload["entries"]:
        assert set(entry) >= {
            "entryId", "cycleId", "asset", "kind",
            "amount", "counterparty", "reference", "createdAt",
        }


def test_wallet_write_contracts_examples_are_valid():
    validate("wallet/propose-swap-request", example("wallet-propose-swap-request.json"))
    validate("wallet/write-accepted", example("wallet-write-accepted-response.json"))
    validate("wallet/jwks", example("wallet-jwks.json"))


def test_propose_swap_request_is_closed_and_requires_expiry():
    payload = json.loads(json.dumps(example("wallet-propose-swap-request.json")))
    payload["counterpart"] = "HGW"

    with pytest.raises(ValidationError):
        validate("wallet/propose-swap-request", payload)

    payload.pop("counterpart")
    payload.pop("expiresAt")

    with pytest.raises(ValidationError):
        validate("wallet/propose-swap-request", payload)
