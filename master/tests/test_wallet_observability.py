"""E2-027: HTTP observable con transporte simulado; sin conexión a wallet."""
import logging
import os
from unittest.mock import patch

import httpx
import pytest

os.environ.setdefault("DATABASE_URL", "sqlite://")
from app.services.wallet_observability import ObservedWalletAsyncClient, wallet_endpoint
from app.services.wallet_client import WalletClient, WalletError


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_routes_never_include_swap_id_or_query():
    assert wallet_endpoint("https://wallet.test/v1/swaps/swp-77/accept?token=secret") == "swaps_accept"
    assert wallet_endpoint("https://wallet.test/v1/balance?api_key=secret") == "balance"
    assert wallet_endpoint("https://wallet.test/v1/swaps/swp-77") == "swaps_get"


@pytest.mark.anyio
async def test_success_and_rate_limit_record_latency_without_secret(caplog):
    seen = []

    async def handler(request):
        seen.append(request.headers.get("X-Api-Key"))
        if request.url.path.endswith("/balance"):
            return httpx.Response(429, headers={"Retry-After": "12"}, text="secret-body")
        return httpx.Response(202, json={"swapId": "swp-77", "status": "accepted"})

    with caplog.at_level(logging.INFO, logger="energyshark.wallet.observability"):
        async with ObservedWalletAsyncClient(transport=httpx.MockTransport(handler)) as client:
            response = await client.post("https://wallet.test/v1/swaps", headers={
                "X-Api-Key": "TOP-SECRET-KEY", "Idempotency-Key": "550e8400-e29b-41d4-a716-446655440000"
            })
            assert response.status_code == 202
            response = await client.get("https://wallet.test/v1/balance", headers={"X-Api-Key": "TOP-SECRET-KEY"})
            assert response.status_code == 429
    assert seen == ["TOP-SECRET-KEY", "TOP-SECRET-KEY"]
    assert "endpoint=swaps_propose" in caplog.text
    assert "outcome=http_429" in caplog.text
    assert "retryAfterSeconds=12" in caplog.text
    assert "latencyMs=" in caplog.text
    assert "TOP-SECRET-KEY" not in caplog.text
    assert "secret-body" not in caplog.text


@pytest.mark.anyio
async def test_timeout_records_safe_outcome(caplog):
    async def handler(request):
        raise httpx.ReadTimeout("secret-credential-in-error")

    with caplog.at_level(logging.INFO, logger="energyshark.wallet.observability"):
        async with ObservedWalletAsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(httpx.ReadTimeout):
                await client.get("https://wallet.test/v1/statement", headers={"X-Api-Key": "TOP-SECRET"})
    assert "outcome=timeout" in caplog.text
    assert "secret-credential-in-error" not in caplog.text
    assert "TOP-SECRET" not in caplog.text


def test_wallet_errors_do_not_repeat_response_body(caplog):
    wallet = WalletClient(base_url="https://wallet.test", api_key="TOP-SECRET")
    response = httpx.Response(503, text="TOP-SECRET")
    with caplog.at_level(logging.ERROR):
        with pytest.raises(WalletError) as caught:
            wallet._handle_response_error(response)
    assert "TOP-SECRET" not in caplog.text
    assert "TOP-SECRET" not in str(caught.value)


@pytest.mark.anyio
async def test_transport_failure_recorded_without_leaking_error(caplog):
    async def handler(request):
        raise httpx.RemoteProtocolError("sensitive-token-in-transport-error")

    with caplog.at_level(logging.INFO, logger="energyshark.wallet.observability"):
        async with ObservedWalletAsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(httpx.RemoteProtocolError):
                await client.get("https://wallet.test/v1/balance")
    assert "outcome=transport_error" in caplog.text
    assert "sensitive-token-in-transport-error" not in caplog.text


def test_wallet_event_is_associated_with_newrelic_application():
    from app.services import wallet_observability as obs
    from unittest.mock import MagicMock

    agent = MagicMock()
    with patch.object(obs, "nr_agent", agent):
        obs.record_wallet_request(method="GET", endpoint="balance", elapsed_ms=12.5, outcome="http_200", status_code=200)
    agent.record_custom_event.assert_called_once()
    assert agent.record_custom_event.call_args.kwargs["application"] is agent.application.return_value


def test_scheduler_registers_newrelic_application(monkeypatch):
    from app.services import wallet_observability as obs
    from unittest.mock import MagicMock

    monkeypatch.setenv("NEW_RELIC_MONITOR_MODE", "true")
    agent = MagicMock()
    with patch.object(obs, "nr_agent", agent):
        obs.register_wallet_worker_application()
    agent.register_application.assert_called_once_with(timeout=0.0)


def test_scheduler_registration_is_disabled_with_monitor_off(monkeypatch):
    from app.services import wallet_observability as obs
    from unittest.mock import MagicMock

    monkeypatch.setenv("NEW_RELIC_MONITOR_MODE", "false")
    agent = MagicMock()
    with patch.object(obs, "nr_agent", agent):
        obs.register_wallet_worker_application()
    agent.register_application.assert_not_called()


def test_scheduler_newrelic_errors_do_not_interrupt(monkeypatch):
    from app.services import wallet_observability as obs
    from unittest.mock import MagicMock

    monkeypatch.setenv("NEW_RELIC_MONITOR_MODE", "true")
    agent = MagicMock()
    agent.register_application.side_effect = RuntimeError("simulado")
    with patch.object(obs, "nr_agent", agent):
        obs.register_wallet_worker_application()
    agent.register_application.assert_called_once_with(timeout=0.0)
