from datetime import datetime, timezone
from uuid import UUID
import pytest
from pydantic import ValidationError
from app.schemas import ProtocolErrorPayload


def valid_error_payload(**overrides):
    payload = {
        "idpk": "550e8400-e29b-41d4-a716-446655440000",
        "msgId": "550e8400-e29b-41d4-a716-446655440001",
        "type": "error",
        "timestamp": "2026-09-16T02:00:00Z",
        "sender": "central",
        "cycleId": "cycle-9431",
        "reason": "CYCLE_UNKNOWN",
        "code": 404,
        "data": {
            "target": "550e8400-e29b-41d4-a716-446655440002",
            "message": "El ciclo no existe",
        },
    }

    payload.update(overrides)
    return payload

#Un CYCLE_UNKNOWN correctamente formado debe ser aceptado.
def test_valid_cycle_unknown_error():
    error = ProtocolErrorPayload.model_validate(valid_error_payload())

    assert error.reason == "CYCLE_UNKNOWN"
    assert error.code == 404
    assert isinstance(error.data.target, UUID)

#CYCLE_EXPIRED debe utilizar code 410.
def test_valid_cycle_expired_error():
    payload = valid_error_payload(
        reason="CYCLE_EXPIRED",
        code=410,
    )

    error = ProtocolErrorPayload.model_validate(payload)

    assert error.reason == "CYCLE_EXPIRED"
    assert error.code == 410

#PRICE_ABOVE_CAP debe incluir data.cap.
def test_price_above_cap_requires_cap():
    payload = valid_error_payload(
        reason="PRICE_ABOVE_CAP",
        code=422,
    )

    with pytest.raises(ValidationError):
        ProtocolErrorPayload.model_validate(payload)

#PRICE_ABOVE_CAP es válido cuando incluye data.cap.
def test_valid_price_above_cap():
    payload = valid_error_payload(
        reason="PRICE_ABOVE_CAP",
        code=422,
        data={
            "target": "550e8400-e29b-41d4-a716-446655440002",
            "message": "Precio sobre el límite",
            "cap": 220.5,
        },
    )

    error = ProtocolErrorPayload.model_validate(payload)

    assert error.data.cap == 220.5

#OVER_CAPACITY debe incluir data.spare.
def test_over_capacity_requires_spare():
    payload = valid_error_payload(
        reason="OVER_CAPACITY",
        code=409,
    )

    with pytest.raises(ValidationError):
        ProtocolErrorPayload.model_validate(payload)

#OVER_CAPACITY es válido cuando incluye data.spare.
def test_valid_over_capacity():
    payload = valid_error_payload(
        reason="OVER_CAPACITY",
        code=409,
        data={
            "target": "550e8400-e29b-41d4-a716-446655440002",
            "message": "Capacidad excedida",
            "spare": 320,
        },
    )

    error = ProtocolErrorPayload.model_validate(payload)

    assert error.data.spare == 320

#Un reason con un code incorrecto debe rechazarse.
def test_reject_wrong_reason_code_pair():
    payload = valid_error_payload(
        reason="CYCLE_UNKNOWN",
        code=410,
    )

    with pytest.raises(ValidationError):
        ProtocolErrorPayload.model_validate(payload)

#timestamp debe incluir zona horaria.
def test_reject_timestamp_without_timezone():
    payload = valid_error_payload(
        timestamp="2026-09-16T02:00:00",
    )

    with pytest.raises(ValidationError):
        ProtocolErrorPayload.model_validate(payload)

#data.target conserva el msgId del mensaje que originó el error.
def test_target_identifies_original_message():
    target = "550e8400-e29b-41d4-a716-446655440002"

    error = ProtocolErrorPayload.model_validate(valid_error_payload())

    assert str(error.data.target) == target