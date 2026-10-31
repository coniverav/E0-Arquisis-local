"""E2-061: heartbeat market es independiente de que existan ofertas."""
from unittest.mock import MagicMock, patch

import observability


def test_market_heartbeat_emits_newrelic_event_without_message():
    agent = MagicMock()
    with patch.object(observability, "nr_agent", agent):
        observability.record_consumer_heartbeat("market")
    args, kwargs = agent.record_custom_event.call_args
    assert args == ("EnergySharkBrokerHeartbeat", {"consumer": "market"})
    assert kwargs["application"] is agent.application.return_value


def test_heartbeat_rejects_unknown_consumer():
    import pytest
    with pytest.raises(ValueError):
        observability.record_consumer_heartbeat("url-secret")


def test_registers_background_application_when_monitoring_enabled(monkeypatch):
    monkeypatch.setenv("NEW_RELIC_MONITOR_MODE", "true")
    agent = MagicMock()
    with patch.object(observability, "nr_agent", agent):
        observability.register_connector_application()
    agent.register_application.assert_called_once_with(timeout=0.0)


def test_skips_registration_when_monitoring_disabled(monkeypatch):
    monkeypatch.setenv("NEW_RELIC_MONITOR_MODE", "false")
    agent = MagicMock()
    with patch.object(observability, "nr_agent", agent):
        observability.register_connector_application()
    agent.register_application.assert_not_called()


def test_registration_error_does_not_interrupt_connector(monkeypatch):
    monkeypatch.setenv("NEW_RELIC_MONITOR_MODE", "true")
    agent = MagicMock()
    agent.register_application.side_effect = RuntimeError("test")
    with patch.object(observability, "nr_agent", agent):
        observability.register_connector_application()
    agent.register_application.assert_called_once_with(timeout=0.0)
