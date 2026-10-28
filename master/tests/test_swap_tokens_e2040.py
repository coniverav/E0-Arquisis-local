"""E2-040: verificación ES256/JWKS del swapToken antes de aceptar."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json

from cryptography.hazmat.primitives.asymmetric import ec
import jwt
import pytest
from jwt.algorithms import ECAlgorithm

from app.services.swap_tokens import (
    SwapTokenError,
    clear_jwks_cache,
    verify_swap_token,
)
from tests.contracts_v3 import example, validate


PRIVATE_KEY = ec.generate_private_key(ec.SECP256R1())
PUBLIC_JWK = json.loads(ECAlgorithm.to_jwk(PRIVATE_KEY.public_key()))
PUBLIC_JWK.update({"kid": "wallet-test-kid", "use": "sig", "alg": "ES256"})
JWKS = {"keys": [PUBLIC_JWK]}

SWAP_ID = "swp-77"
PROPOSER = "KLD"
GIVE_ASSET = "KWH"
GIVE_AMOUNT = Decimal("7300")
TAKE_ASSET = "CR"
TAKE_AMOUNT = Decimal("2409000")
MEMO = "KLD-9431"


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_jwks_cache()
    yield
    clear_jwks_cache()


def _claims(**overrides):
    now = datetime.now(timezone.utc)
    claims = {
        "iss": "wallet.energyshark",
        "sub": SWAP_ID,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=2)).timestamp()),
        "proposer": PROPOSER,
        "give": {"asset": GIVE_ASSET, "amount": int(GIVE_AMOUNT)},
        "take": {"asset": TAKE_ASSET, "amount": int(TAKE_AMOUNT)},
        "memo": MEMO,
    }
    claims.update(overrides)
    return claims


def _token(**overrides):
    return jwt.encode(
        _claims(**overrides),
        PRIVATE_KEY,
        algorithm="ES256",
        headers={"kid": "wallet-test-kid"},
    )


def _verify(token, **overrides):
    params = {
        "swap_id": SWAP_ID,
        "proposer": PROPOSER,
        "give_asset": GIVE_ASSET,
        "give_amount": GIVE_AMOUNT,
        "take_asset": TAKE_ASSET,
        "take_amount": TAKE_AMOUNT,
        "memo": MEMO,
        "jwks_url": "https://wallet.test/.well-known/jwks.json",
        "jwks_fetcher": lambda _url: JWKS,
    }
    params.update(overrides)
    return verify_swap_token(token, **params)


def test_valid_token_is_accepted_and_matches_contract():
    claims = _verify(_token())

    assert claims["sub"] == SWAP_ID
    assert claims["proposer"] == PROPOSER
    validate("token/swap-token-claims", claims)


def test_official_claim_example_is_contract_valid():
    claims = example("swap-token-claims.json")
    validate("token/swap-token-claims", claims)
    assert claims["iss"] == "wallet.energyshark"


def test_signature_from_another_key_is_rejected():
    other_key = ec.generate_private_key(ec.SECP256R1())
    token = jwt.encode(
        _claims(),
        other_key,
        algorithm="ES256",
        headers={"kid": "wallet-test-kid"},
    )

    with pytest.raises(SwapTokenError) as error:
        _verify(token)

    assert error.value.reason == "SWAP_TOKEN_SIGNATURE"


def test_expired_token_is_rejected():
    past = int((datetime.now(timezone.utc) - timedelta(seconds=5)).timestamp())

    with pytest.raises(SwapTokenError) as error:
        _verify(_token(exp=past))

    assert error.value.reason == "SWAP_TOKEN_EXPIRED"


def test_expiry_is_checked_against_supplied_now():
    token = _token()
    later = datetime.now(timezone.utc) + timedelta(minutes=5)

    with pytest.raises(SwapTokenError) as error:
        _verify(token, now=later)

    assert error.value.reason == "SWAP_TOKEN_EXPIRED"


def test_wrong_sub_is_rejected():
    with pytest.raises(SwapTokenError) as error:
        _verify(_token(sub="swp-other"))

    assert error.value.reason == "SWAP_TOKEN_SUB_MISMATCH"


def test_wrong_proposer_is_rejected():
    with pytest.raises(SwapTokenError) as error:
        _verify(_token(proposer="COR"))

    assert error.value.reason == "SWAP_TOKEN_PROPOSER_MISMATCH"


def test_wrong_give_is_rejected():
    with pytest.raises(SwapTokenError) as error:
        _verify(_token(give={"asset": "KWH", "amount": 7200}))

    assert error.value.reason == "SWAP_TOKEN_GIVE_MISMATCH"


def test_wrong_take_is_rejected():
    with pytest.raises(SwapTokenError) as error:
        _verify(_token(take={"asset": "CR", "amount": 2408000}))

    assert error.value.reason == "SWAP_TOKEN_TAKE_MISMATCH"


def test_wrong_memo_is_rejected():
    with pytest.raises(SwapTokenError) as error:
        _verify(_token(memo="KLD-0000"))

    assert error.value.reason == "SWAP_TOKEN_MEMO_MISMATCH"


def test_non_es256_algorithm_is_rejected():
    token = jwt.encode(
        _claims(),
        "shared-secret",
        algorithm="HS256",
        headers={"kid": "wallet-test-kid"},
    )

    with pytest.raises(SwapTokenError) as error:
        _verify(token)

    assert error.value.reason == "SWAP_TOKEN_ALGORITHM"


def test_unknown_kid_is_rejected():
    token = jwt.encode(
        _claims(),
        PRIVATE_KEY,
        algorithm="ES256",
        headers={"kid": "other-kid"},
    )

    with pytest.raises(SwapTokenError) as error:
        _verify(token)

    assert error.value.reason == "SWAP_TOKEN_UNKNOWN_KID"


def test_wrong_issuer_is_rejected():
    with pytest.raises(SwapTokenError) as error:
        _verify(_token(iss="other-issuer"))

    assert error.value.reason == "SWAP_TOKEN_ISSUER"


def test_missing_token_is_rejected():
    with pytest.raises(SwapTokenError) as error:
        _verify("")

    assert error.value.reason == "SWAP_TOKEN_MISSING"


def test_jwks_is_cached_between_calls():
    calls = []

    def fetcher(url):
        calls.append(url)
        return JWKS

    _verify(_token(), jwks_fetcher=fetcher)
    _verify(_token(), jwks_fetcher=fetcher)

    assert len(calls) == 1


def test_jwks_fetch_failure_is_reported():
    def failing(_url):
        raise RuntimeError("network down")

    with pytest.raises(RuntimeError):
        _verify(_token(), jwks_fetcher=failing)
