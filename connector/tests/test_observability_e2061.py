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
