from protocol.routing import select_master_url

DEFAULT_URL = "http://master:8000/internal/events"
ERROR_URL = "http://master:8000/internal/protocol/errors"

#Un mensaje error debe enviarse al endpoint específico de errores.
def test_error_routes_to_error_endpoint():
    payload = {
        "type": "error",
    }

    result = select_master_url(
        payload,
        default_url=DEFAULT_URL,
        error_url=ERROR_URL,
    )

    assert result == ERROR_URL

#Los demás mensajes continúan usando el endpoint general.
def test_non_error_routes_to_default_endpoint():
    payload = {
        "type": "status-statement",
    }

    result = select_master_url(
        payload,
        default_url=DEFAULT_URL,
        error_url=ERROR_URL,
    )

    assert result == DEFAULT_URL