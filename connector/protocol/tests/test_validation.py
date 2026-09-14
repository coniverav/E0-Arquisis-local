import pytest
from protocol.parser import MissingMsgIdError
from protocol.validation import validate_message, validate_payload
from protocol.parser import parse_envelope

#Comprueba el flujo general con un mensaje válido.
def test_valid_message():
    payload = {
        "idpk": "550e8400-e29b-41d4-a716-446655440000",
        "msgId": "550e8400-e29b-41d4-a716-446655440001",
        "type": "ack",
        "timestamp": "2026-09-13T20:00:00Z",
        "sender": "central",
        "data": {
            "target": "550e8400-e29b-41d4-a716-446655440002"
        },
    }

    message = parse_envelope(payload)
    result = validate_message(message)

    assert result.valid is True
    assert result.reason is None
    assert result.code is None

#idpk y msgId deben ser distintos según el protocolo.
def test_reject_same_idpk_and_msgid():
    same_uuid = "550e8400-e29b-41d4-a716-446655440000"

    payload = {
        "idpk": same_uuid,
        "msgId": same_uuid,
        "type": "status-statement",
        "timestamp": "2026-09-13T20:00:00Z",
        "sender": "central",
        "data": {},
    }

    message = parse_envelope(payload)
    result = validate_message(message)

    assert result.valid is False
    assert result.reason == "IDPK_EQUALS_MSGID"
    assert result.code == 422

#Un type que no pertenece al protocolo debe rechazarse.
def test_reject_unknown_type():
    payload = {
        "idpk": "550e8400-e29b-41d4-a716-446655440000",
        "msgId": "550e8400-e29b-41d4-a716-446655440001",
        "type": "unknown-message",
        "timestamp": "2026-09-13T20:00:00Z",
        "sender": "central",
        "data": {},
    }

    message = parse_envelope(payload)
    result = validate_message(message)

    assert result.valid is False
    assert result.reason == "UNKNOWN_TYPE"
    assert result.code == 400

#Un envelope parseable al que le falta un campo obligatorio es malformed.
def test_reject_missing_required_field():
    payload = {
        "msgId": "550e8400-e29b-41d4-a716-446655440001",
        "type": "status-statement",
        "timestamp": "2026-09-13T20:00:00Z",
        "sender": "central",
        "data": {},
    }

    message, result = validate_payload(payload)

    assert message is None
    assert result.valid is False
    assert result.reason == "MALFORMED_MESSAGE"
    assert result.code == 422

#Un UUID inválido hace que el contenido del envelope sea malformed.
def test_reject_invalid_uuid():
    payload = {
        "idpk": "no-es-un-uuid",
        "msgId": "550e8400-e29b-41d4-a716-446655440001",
        "type": "status-statement",
        "timestamp": "2026-09-13T20:00:00Z",
        "sender": "central",
        "data": {},
    }

    message, result = validate_payload(payload)

    assert result.valid is False
    assert message is None
    assert result.reason == "MALFORMED_MESSAGE"
    assert result.code == 422

#Sin msgId no existe un mensaje válido al cual responder.
def test_missing_msgid_is_not_malformed_nack():
    payload = {
        "idpk": "550e8400-e29b-41d4-a716-446655440000",
        "type": "status-statement",
        "timestamp": "2026-09-13T20:00:00Z",
        "sender": "central",
        "data": {},
    }

    with pytest.raises(MissingMsgIdError):
        validate_payload(payload)

#Valida la estructura propia de un status-statement.
def test_valid_status_statement_content():
    payload = {
        "idpk": "550e8400-e29b-41d4-a716-446655440000",
        "msgId": "550e8400-e29b-41d4-a716-446655440001",
        "type": "status-statement",
        "timestamp": "2026-09-13T20:00:00Z",
        "sender": "central",
        "cycleId": "cycle-1",
        "data": {
            "energy": {
                "generationCapacity": 1000,
                "consumption": 700,
                "generationCost": 200,
            },
            "validUntil": "2026-09-13T22:00:00Z",
        },
    }

    _, result = validate_payload(payload)

    assert result.valid is True

#generationCost es obligatorio dentro de data.energy.
def test_reject_invalid_status_statement_content():
    payload = {
        "idpk": "550e8400-e29b-41d4-a716-446655440000",
        "msgId": "550e8400-e29b-41d4-a716-446655440001",
        "type": "status-statement",
        "timestamp": "2026-09-13T20:00:00Z",
        "sender": "central",
        "cycleId": "cycle-1",
        "data": {
            "energy": {
                "generationCapacity": 1000,
                "consumption": 700,
            },
            "validUntil": "2026-09-13T22:00:00Z",
        },
    }

    _, result = validate_payload(payload)

    assert result.valid is False
    assert result.reason == "MALFORMED_MESSAGE"
    assert result.code == 422

#Un ACK debe referenciar mediante target al msgId reconocido.
def test_valid_ack_content():
    payload = {
        "idpk": "550e8400-e29b-41d4-a716-446655440000",
        "msgId": "550e8400-e29b-41d4-a716-446655440001",
        "type": "ack",
        "timestamp": "2026-09-13T20:00:00Z",
        "sender": "central",
        "data": {
            "target": "550e8400-e29b-41d4-a716-446655440002"
        },
    }

    _, result = validate_payload(payload)

    assert result.valid is True

#reason y code deben corresponder a la misma causa.
def test_reject_nack_with_wrong_code():
    payload = {
        "idpk": "550e8400-e29b-41d4-a716-446655440000",
        "msgId": "550e8400-e29b-41d4-a716-446655440001",
        "type": "nack",
        "timestamp": "2026-09-13T20:00:00Z",
        "sender": "central",
        "reason": "UNKNOWN_TYPE",
        "code": 422,
        "data": {
            "target": "550e8400-e29b-41d4-a716-446655440002",
            "message": "Tipo desconocido",
        },
    }

    _, result = validate_payload(payload)

    assert result.valid is False
    assert result.reason == "MALFORMED_MESSAGE"
    assert result.code == 422

#OVER_CAPACITY debe incluir el campo legible por máquina spare.
def test_reject_over_capacity_without_spare():
    payload = {
        "idpk": "550e8400-e29b-41d4-a716-446655440000",
        "msgId": "550e8400-e29b-41d4-a716-446655440001",
        "type": "error",
        "timestamp": "2026-09-13T20:00:00Z",
        "sender": "central",
        "cycleId": "cycle-1",
        "reason": "OVER_CAPACITY",
        "code": 409,
        "data": {
            "target": "550e8400-e29b-41d4-a716-446655440002",
            "message": "give excede la capacidad disponible",
        },
    }

    _, result = validate_payload(payload)

    assert result.code == 422
    assert result.valid is False
    assert result.reason == "MALFORMED_MESSAGE"

#status-statement requiere cycleId.
def test_reject_missing_cycle_id():
    payload = {
        "idpk": "550e8400-e29b-41d4-a716-446655440000",
        "msgId": "550e8400-e29b-41d4-a716-446655440001",
        "type": "status-statement",
        "timestamp": "2026-09-13T20:00:00Z",
        "sender": "central",
        "data": {
            "energy": {
                "generationCapacity": 1000,
                "consumption": 700,
                "generationCost": 200,
            },
            "validUntil": "2026-09-13T22:00:00Z",
        },
    }

    _, result = validate_payload(payload)

    assert result.valid is False
    assert result.reason == "MALFORMED_MESSAGE"
    assert result.code == 422

#Un mensaje no puede identificarse como central y ciudad a la vez.
def test_reject_sender_and_city_id_together():
    payload = {
        "idpk": "550e8400-e29b-41d4-a716-446655440000",
        "msgId": "550e8400-e29b-41d4-a716-446655440001",
        "type": "ack",
        "timestamp": "2026-09-13T20:00:00Z",
        "sender": "central",
        "cityId": "COR",
        "data": {
            "target": "550e8400-e29b-41d4-a716-446655440002",
        },
    }

    _, result = validate_payload(payload)

    assert result.valid is False
    assert result.reason == "MALFORMED_MESSAGE"

#El timestamp debe incluir zona horaria.
def test_reject_timestamp_without_timezone():
    payload = {
        "idpk": "550e8400-e29b-41d4-a716-446655440000",
        "msgId": "550e8400-e29b-41d4-a716-446655440001",
        "type": "ack",
        "timestamp": "2026-09-13T20:00:00",
        "sender": "central",
        "data": {
            "target": "550e8400-e29b-41d4-a716-446655440002",
        },
    }

    _, result = validate_payload(payload)

    assert result.valid is False
    assert result.reason == "MALFORMED_MESSAGE"

#quantity puede ser negativa según el protocolo.
def test_valid_demand_statement():
    payload = {
        "idpk": "550e8400-e29b-41d4-a716-446655440000",
        "msgId": "550e8400-e29b-41d4-a716-446655440001",
        "type": "demand-statement",
        "timestamp": "2026-09-13T20:00:00Z",
        "sender": "central",
        "cycleId": "cycle-1",
        "data": {
            "balance": {
                "quantity": -300,
                "valuePerKwh": 210,
            }
        },
    }

    _, result = validate_payload(payload)

    assert result.valid is True