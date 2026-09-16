from datetime import datetime, timezone
from uuid import UUID

from protocol.parser import parse_envelope
from protocol.responses import build_ack, should_send_ack

#El ACK debe apuntar al msgId recibido y usar un msgId nuevo.
def test_build_ack():
    payload = {
        "idpk": "550e8400-e29b-41d4-a716-446655440000",
        "msgId": "550e8400-e29b-41d4-a716-446655440001",
        "type": "status-statement",
        "timestamp": "2026-09-15T20:00:00Z",
        "sender": "central",
        "cycleId": "cycle-1",
        "data": {
            "energy": {
                "generationCapacity": 1000,
                "consumption": 700,
                "generationCost": 200,
            },
            "validUntil": "2026-09-15T22:00:00Z",
        },
    }

    message = parse_envelope(payload)

    ack = build_ack(
        message,
        city_id="COR",
        timestamp=datetime(2026, 9, 15, 21, 0, tzinfo=timezone.utc),
    )

    assert ack["type"] == "ack"
    assert ack["cityId"] == "COR"
    assert ack["data"]["target"] == payload["msgId"]
    assert ack["msgId"] != payload["msgId"]

    #Ambos identificadores generados deben ser UUID válidos.
    UUID(ack["idpk"])
    UUID(ack["msgId"])

#El timestamp del ACK se genera en ISO 8601 UTC.
def test_ack_has_expected_timestamp():
    payload = {
        "idpk": "550e8400-e29b-41d4-a716-446655440000",
        "msgId": "550e8400-e29b-41d4-a716-446655440001",
        "type": "request",
        "timestamp": "2026-09-15T20:00:00Z",
        "cityId": "COR",
        "data": {
            "ask": "status-statement",
        },
    }

    message = parse_envelope(payload)

    ack = build_ack(
        message,
        city_id="COR",
        timestamp=datetime(2026, 9, 15, 21, 0, tzinfo=timezone.utc),
    )

    assert ack["timestamp"] == "2026-09-15T21:00:00Z"

#Un mensaje normal aceptado debe recibir ACK.
def test_should_ack_normal_message():
    payload = {
        "idpk": "550e8400-e29b-41d4-a716-446655440000",
        "msgId": "550e8400-e29b-41d4-a716-446655440001",
        "type": "status-statement",
        "timestamp": "2026-09-15T20:00:00Z",
        "sender": "central",
        "cycleId": "cycle-1",
        "data": {},
    }

    message = parse_envelope(payload)

    assert should_send_ack(message) is True

#Verifica que un ACK recibido no genere otro ACK.
def test_do_not_ack_ack():
    payload = {
        "idpk": "550e8400-e29b-41d4-a716-446655440000",
        "msgId": "550e8400-e29b-41d4-a716-446655440001",
        "type": "ack",
        "timestamp": "2026-09-15T20:00:00Z",
        "sender": "central",
        "data": {
            "target": "550e8400-e29b-41d4-a716-446655440002",
        },
    }

    message = parse_envelope(payload)

    assert should_send_ack(message) is False

#Verifica que un NACK recibido no genere un ACK.
def test_do_not_ack_nack():
    payload = {
        "idpk": "550e8400-e29b-41d4-a716-446655440000",
        "msgId": "550e8400-e29b-41d4-a716-446655440001",
        "type": "nack",
        "timestamp": "2026-09-15T20:00:00Z",
        "sender": "central",
        "reason": "UNKNOWN_TYPE",
        "code": 400,
        "data": {
            "target": "550e8400-e29b-41d4-a716-446655440002",
            "message": "Tipo desconocido",
        },
    }

    message = parse_envelope(payload)

    assert should_send_ack(message) is False

#Verifica que un mensaje error recibido no genere un ACK.
def test_do_not_ack_error():
    payload = {
        "idpk": "550e8400-e29b-41d4-a716-446655440000",
        "msgId": "550e8400-e29b-41d4-a716-446655440001",
        "type": "error",
        "timestamp": "2026-09-15T20:00:00Z",
        "sender": "central",
        "cycleId": "cycle-1",
        "reason": "CYCLE_UNKNOWN",
        "code": 404,
        "data": {
            "target": "550e8400-e29b-41d4-a716-446655440002",
            "message": "Ciclo inexistente",
        },
    }

    message = parse_envelope(payload)

    assert should_send_ack(message) is False