"""E2-047: REST /v1 y JWKS en la raíz, sin solicitudes a Wallet real."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import runpy

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from jwt.algorithms import ECAlgorithm

from app.config import CITY_ID
from app.services import swap_tokens, wallet_client as wallet_client_module
from app.services.wallet_client import WalletClient
from tests.contracts_v3 import example


@pytest.mark.parametrize(
    ("base_url", "configured_jwks", "expected_jwks"),
    [
        ("https://wallet.test/v1", None,
         "https://wallet.test/.well-known/jwks.json"),
        ("https://wallet.test/v1/", "",
         "https://wallet.test/.well-known/jwks.json"),
        ("https://wallet.test/v1", "https://keys.test/wallet/jwks.json",
         "https://keys.test/wallet/jwks.json"),
        ("http://wallet-mock:8080", None,
         "http://wallet-mock:8080/.well-known/jwks.json"),
    ],
)
def test_wallet_config_separates_rest_base_and_jwks_origin(
    monkeypatch, base_url, configured_jwks, expected_jwks,
):
    monkeypatch.setenv("WALLET_BASE_URL", base_url)
    if configured_jwks is None:
        monkeypatch.delenv("WALLET_JWKS_URL", raising=False)
    else:
        monkeypatch.setenv("WALLET_JWKS_URL", configured_jwks)

    # Ejecutar la configuración aislada evita recargar módulos ya importados
    # por otras pruebas de master.
    config_path = Path(__file__).resolve().parents[1] / "app" / "config.py"
    values = runpy.run_path(str(config_path))
    assert values["WALLET_BASE_URL"] == base_url.rstrip("/")
    assert values["WALLET_JWKS_URL"] == expected_jwks
    monkeypatch.setattr(wallet_client_module, "WALLET_BASE_URL", values["WALLET_BASE_URL"])
    assert WalletClient(api_key="test-only").base_url == base_url.rstrip("/")


@pytest.mark.anyio
async def test_wallet_client_uses_v1_for_all_rest_calls(monkeypatch):
    requests = []

    def wallet_http(request):
        requests.append(request)
        assert request.url.host == "wallet.test"
        assert request.headers["X-Api-Key"] == "test-only"
        if request.url.path == "/v1/balance":
            balance = deepcopy(example("wallet-balance-response.json"))
            balance["cityId"] = CITY_ID
            return httpx.Response(200, json=balance)
        if request.url.path == "/v1/statement":
            return httpx.Response(200, json=example("wallet-statement-response.json"))
        if request.method == "GET":
            return httpx.Response(200, json={"swapId": "swp-test", "status": "PROPOSED"})
        assert request.headers["Idempotency-Key"] == "test-idpk"
        return httpx.Response(202, json={"accepted": True})

    observed = wallet_client_module.ObservedWalletAsyncClient
    monkeypatch.setattr(
        wallet_client_module, "ObservedWalletAsyncClient",
        lambda **kwargs: observed(transport=httpx.MockTransport(wallet_http), **kwargs),
    )
    client = WalletClient(base_url="https://wallet.test/v1/", api_key="test-only")
    assert (await client.get_balance()).city_id == CITY_ID
    assert isinstance((await client.get_statement(cycle_id="cycle-test")).entries, list)
    assert (await client.get_swap("swp-test"))["swapId"] == "swp-test"
    assert (await client.propose_swap(
        idempotency_key="test-idpk", give_asset="KWH", give_amount=Decimal("1"),
        take_asset="CR", take_amount=Decimal("10"), counterpart_city_id="MET",
        memo="offer-test", expires_at=datetime.now(timezone.utc) + timedelta(minutes=1),
    )).accepted_for_processing
    assert (await client.accept_swap("swp-test", idempotency_key="test-idpk")).accepted_for_processing
    assert (await client.cancel_swap("swp-test", idempotency_key="test-idpk")).accepted_for_processing
    assert [request.url.path for request in requests] == [
        "/v1/balance", "/v1/statement", "/v1/swaps/swp-test",
        "/v1/swaps", "/v1/swaps/swp-test/accept", "/v1/swaps/swp-test/cancel",
    ]
    assert requests[1].url.params["cycleId"] == "cycle-test"


def test_swap_token_uses_jwks_root_without_v1(monkeypatch):
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_jwk = json.loads(ECAlgorithm.to_jwk(private_key.public_key()))
    public_jwk.update(kid="wallet-test-kid", use="sig", alg="ES256")
    now = datetime.now(timezone.utc)
    token = jwt.encode({
        "iss": swap_tokens.WALLET_ISSUER,
        "sub": "swp-test",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=1)).timestamp()),
        "proposer": "MET",
        "give": {"asset": "KWH", "amount": 1},
        "take": {"asset": "CR", "amount": 10},
        "memo": "offer-test",
    }, private_key, algorithm="ES256", headers={"kid": "wallet-test-kid"})

    requested_urls = []

    def fetch_jwks(url):
        requested_urls.append(url)
        return {"keys": [public_jwk]}

    monkeypatch.setattr(
        swap_tokens, "WALLET_JWKS_URL", "https://wallet.test/.well-known/jwks.json",
    )
    swap_tokens.clear_jwks_cache()
    try:
        claims = swap_tokens.verify_swap_token(
            token, swap_id="swp-test", proposer="MET",
            give_asset="KWH", give_amount=Decimal("1"),
            take_asset="CR", take_amount=Decimal("10"),
            memo="offer-test", now=now, jwks_fetcher=fetch_jwks,
        )
        assert claims["sub"] == "swp-test"
        assert requested_urls == ["https://wallet.test/.well-known/jwks.json"]
    finally:
        swap_tokens.clear_jwks_cache()
