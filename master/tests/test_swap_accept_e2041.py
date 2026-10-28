"""E2-041: aceptación asíncrona idempotente de swaps.

El test recorre el flujo real hasta el ingress: `bid` en la outbox,
`bid-accepted` con token ES256, y luego el worker. La wallet se sustituye por
un doble para verificar 202, timeout y 4xx sin red.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import asyncio
import json
from uuid import uuid4

from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
import jwt
from jwt.algorithms import ECAlgorithm
import pytest
from sqlmodel import Session, select

from app.database import engine, run_migrations
from app.main import app
from app.models import MarketBid, SwapRecord
from app.services import swap_tokens
from app.services.buy_bids import enqueue_buy_bid
from app.services.swap_accept import accept_pending_swaps
from app.services.wallet_client import (
    WalletError,
    WalletRateLimitError,
    WalletSwapWriteResponse,
    WalletTimeoutError,
)
from tests.test_buy_bids import seed


PRIVATE_KEY = ec.generate_private_key(ec.SECP256R1())
PUBLIC_JWK = json.loads(ECAlgorithm.to_jwk(PRIVATE_KEY.public_key()))
PUBLIC_JWK.update({"kid": "wallet-test-kid", "use": "sig", "alg": "ES256"})
JWKS = {"keys": [PUBLIC_JWK]}

QUANTITY = 6
PRICE = 12
AMOUNT = QUANTITY * PRICE


def setup_module():
    run_migrations()


@pytest.fixture(autouse=True)
def _jwks(monkeypatch):
    swap_tokens.clear_jwks_cache()
    monkeypatch.setattr(
        swap_tokens, "_fetch_jwks", lambda *_args, **_kwargs: JWKS,
    )
    yield
    swap_tokens.clear_jwks_cache()


def _token(
    *,
    swap_id: str,
    proposer: str,
    memo: str,
    quantity: int = QUANTITY,
    amount: int = AMOUNT,
    expires_in: int = 60,
) -> str:
    now = datetime.now(timezone.utc)
    claims = {
        "iss": "wallet.energyshark",
        "sub": swap_id,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=expires_in)).timestamp()),
        "proposer": proposer,
        "give": {"asset": "KWH", "amount": quantity},
        "take": {"asset": "CR", "amount": amount},
        "memo": memo,
    }
    return jwt.encode(
        claims, PRIVATE_KEY, algorithm="ES256",
        headers={"kid": "wallet-test-kid"},
    )


def _setup_bid():
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        cycle, offer = seed(session, now=now)
        bid = enqueue_buy_bid(
            session, cycle_id=cycle, offer_id=offer,
            quantity=QUANTITY, idpk=uuid4(), now=now,
        )
    return cycle, offer, bid


def _acceptance(
    cycle,
    offer,
    bid,
    *,
    token: str,
    swap_id: str = "swp-77",
):
    return {
        "idpk": str(uuid4()),
        "msgId": str(uuid4()),
        "type": "bid-accepted",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "cityId": "HGW",
        "cycleId": cycle,
        "data": {
            "target": bid["msgId"],
            "offerId": offer,
            "quantity": QUANTITY,
            "pricePerEnergy": PRICE,
            "amount": AMOUNT,
            "swapId": swap_id,
            "swapToken": token,
            "expiresAt": (
                datetime.now(timezone.utc) + timedelta(minutes=2)
            ).isoformat(),
        },
    }


class FakeWallet:
    def __init__(self, outcomes=None):
        self.outcomes = list(outcomes or [])
        self.calls = []

    async def execute_accept_swap(self, session, record):
        self.calls.append((record.swap_id, record.accept_idempotency_key))
        outcome = self.outcomes.pop(0) if self.outcomes else "ok"
        if outcome == "timeout":
            raise WalletTimeoutError("timeout")
        if outcome == "rate":
            raise WalletRateLimitError("rate", status_code=429, retry_after=1)
        if outcome == "reject":
            raise WalletError("rejected", status_code=409)
        return WalletSwapWriteResponse(
            status_code=202,
            data={"swapId": record.swap_id},
            idempotency_key=record.accept_idempotency_key,
            accepted_for_processing=True,
        )


def _run(wallet):
    with Session(engine) as session:
        stats = asyncio.run(
            accept_pending_swaps(session, wallet_service=wallet)
        )
        record = session.exec(select(SwapRecord)).first()
        if record is not None:
            session.refresh(record)
        return stats, record


def _post(client, cycle, offer, bid, *, token=None):
    token = token or _token(
        swap_id="swp-77", proposer="HGW", memo=offer,
    )
    response = client.post(
        "/internal/messages",
        json=_acceptance(cycle, offer, bid, token=token),
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ok", response.text
    return response


def test_valid_token_accepts_with_durable_key_and_202_is_not_settled():
    cycle, offer, bid = _setup_bid()
    client = TestClient(app)
    _post(client, cycle, offer, bid)

    wallet = FakeWallet()
    stats, record = _run(wallet)

    assert stats["accepted"] == 1
    assert len(wallet.calls) == 1
    assert record.role == "ACCEPTOR"
    assert record.swap_id == "swp-77"
    assert record.source_bid_idpk == bid["idpk"]
    assert record.give_asset == "CR"
    assert record.give_amount == Decimal(AMOUNT)
    assert record.take_asset == "KWH"
    assert record.take_amount == Decimal(QUANTITY)
    assert record.accept_idempotency_key == record.idpk
    assert record.accept_attempts == 1
    assert record.status == "ACCEPT_SUBMITTED"
    assert record.settled_at is None
    assert record.swap_token
    assert record.expires_at is not None


def test_second_run_does_not_create_a_second_swap_or_call():
    cycle, offer, bid = _setup_bid()
    client = TestClient(app)
    _post(client, cycle, offer, bid)

    wallet = FakeWallet()
    _run(wallet)
    stats, record = _run(wallet)

    assert stats["scanned"] == 0
    assert stats["accepted"] == 0
    assert len(wallet.calls) == 1
    with Session(engine) as session:
        records = session.exec(select(SwapRecord)).all()
    assert len(records) == 1
    assert records[0].id == record.id


def test_timeout_retries_with_same_idempotency_key():
    cycle, offer, bid = _setup_bid()
    client = TestClient(app)
    _post(client, cycle, offer, bid)

    wallet = FakeWallet(["timeout", "ok"])
    stats, record = _run(wallet)
    assert stats["retry"] == 1
    assert record.status == "ACCEPT_SUBMITTED"
    assert record.accept_error == "WALLET_TIMEOUT"
    assert record.accept_attempts == 1
    first_key = record.accept_idempotency_key

    stats, record = _run(wallet)
    assert stats["accepted"] == 1
    assert record.accept_attempts == 2
    assert record.accept_error is None
    assert [key for _, key in wallet.calls] == [first_key, first_key]


def test_rate_limit_is_retryable():
    cycle, offer, bid = _setup_bid()
    client = TestClient(app)
    _post(client, cycle, offer, bid)

    wallet = FakeWallet(["rate"])
    stats, record = _run(wallet)

    assert stats["retry"] == 1
    assert record.accept_error == "WALLET_RATE_LIMIT:1"


def test_terminal_4xx_marks_rejected_and_does_not_retry():
    cycle, offer, bid = _setup_bid()
    client = TestClient(app)
    _post(client, cycle, offer, bid)

    wallet = FakeWallet(["reject"])
    stats, record = _run(wallet)
    assert stats["rejected"] == 1
    assert record.status == "REJECTED"
    assert record.rejection_reason == "WALLET_409"

    stats, _ = _run(wallet)
    assert stats["accepted"] == 0
    assert len(wallet.calls) == 1


def test_invalid_token_never_reaches_wallet():
    cycle, offer, bid = _setup_bid()
    client = TestClient(app)
    wrong_memo = _token(
        swap_id="swp-77", proposer="HGW", memo="HGW-otra-oferta",
    )
    _post(client, cycle, offer, bid, token=wrong_memo)

    wallet = FakeWallet()
    stats, record = _run(wallet)

    assert stats["rejected"] == 1
    assert len(wallet.calls) == 0
    assert record.status == "REJECTED"
    assert record.rejection_reason == "SWAP_TOKEN_MEMO_MISMATCH"
    assert record.accept_idempotency_key is None


def test_jwks_outage_is_retryable_and_not_terminal(monkeypatch):
    cycle, offer, bid = _setup_bid()
    client = TestClient(app)
    _post(client, cycle, offer, bid)

    def unavailable(*_args, **_kwargs):
        raise swap_tokens.SwapTokenError("SWAP_TOKEN_JWKS_UNAVAILABLE")

    monkeypatch.setattr(swap_tokens, "_fetch_jwks", unavailable)

    wallet = FakeWallet()
    with Session(engine) as session:
        stats = asyncio.run(
            accept_pending_swaps(session, wallet_service=wallet)
        )
    assert stats["retry"] == 1
    assert stats["rejected"] == 0
    with Session(engine) as session:
        assert session.exec(select(SwapRecord)).all() == []

    # Cuando JWKS vuelve, el mismo bid se acepta sin intervención manual.
    monkeypatch.setattr(
        swap_tokens, "_fetch_jwks", lambda *_args, **_kwargs: JWKS,
    )
    stats, record = _run(wallet)
    assert stats["accepted"] == 1
    assert record.status == "ACCEPT_SUBMITTED"
    assert len(wallet.calls) == 1


def test_expired_bid_acceptance_never_reaches_wallet():
    cycle, offer, bid = _setup_bid()
    client = TestClient(app)
    expired = _token(
        swap_id="swp-77", proposer="HGW", memo=offer, expires_in=-5,
    )
    _post(client, cycle, offer, bid, token=expired)

    wallet = FakeWallet()
    stats, record = _run(wallet)

    assert stats["rejected"] == 1
    assert len(wallet.calls) == 0
    assert record.status == "REJECTED"
    assert "EXPIRED" in record.rejection_reason
