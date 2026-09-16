from datetime import datetime, timezone
from protocol.handler import plan_protocol_response


TEST_TIME = datetime(2026, 9, 15, 21, 0, tzinfo=timezone.utc)

#Un mensaje válido normal debe producir un ACK.
def test_valid_message_plans_ack():
    payload = {
        "idpk": "550e8400-e29b-41d4-a716-446655440000",
        "msgId": "550e8400-e29b-41d4-a716-446655440001",
        "type": "status-statement",
        "timestamp": "2026-09-15T20:00:00Z",
        "sender": "central",
        "cycleId": "cycle-9431",
        "data": {
            "energy": {
                "generationCapacity": 1000,
                "consumption": 700,
                "generationCost": 200,
            },
            "validUntil": "2026-09-15T22:00:00Z",
        },
    }

    result = plan_protocol_response(
        payload,
        city_id="COR",
        timestamp=TEST_TIME,
    )

    assert result.action == "ack"
    assert result.response is not None
    assert result.response["type"] == "ack"
    assert result.response["data"]["target"] == payload["msgId"]

#Un type desconocido debe producir UNKNOWN_TYPE.
def test_unknown_type_plans_nack():
    payload = {
        "idpk": "550e8400-e29b-41d4-a716-446655440000",
        "msgId": "550e8400-e29b-41d4-a716-446655440001",
        "type": "something-unknown",
        "timestamp": "2026-09-15T20:00:00Z",
        "sender": "central",
        "data": {},
    }

    result = plan_protocol_response(
        payload,
        city_id="COR",
        timestamp=TEST_TIME,
    )

    assert result.action == "nack"
    assert result.response["reason"] == "UNKNOWN_TYPE"
    assert result.response["code"] == 400
    assert result.response["data"]["target"] == payload["msgId"]

#Un envelope identificable pero incompleto debe producir MALFORMED_MESSAGE.
def test_missing_required_field_plans_malformed_nack():
    payload = {
        "msgId": "550e8400-e29b-41d4-a716-446655440001",
        "type": "status-statement",
        "timestamp": "2026-09-15T20:00:00Z",
        "sender": "central",
        "data": {},
    }

    result = plan_protocol_response(
        payload,
        city_id="COR",
        timestamp=TEST_TIME,
    )

    assert result.action == "nack"
    assert result.response["reason"] == "MALFORMED_MESSAGE"
    assert result.response["code"] == 422
    assert result.response["data"]["target"] == payload["msgId"]

#idpk igual a msgId debe producir IDPK_EQUALS_MSGID.
def test_same_idpk_and_msgid_plans_nack():
    identifier = "550e8400-e29b-41d4-a716-446655440001"

    payload = {
        "idpk": identifier,
        "msgId": identifier,
        "type": "request",
        "timestamp": "2026-09-15T20:00:00Z",
        "cityId": "COR",
        "data": {
            "ask": "status-statement",
        },
    }

    result = plan_protocol_response(
        payload,
        city_id="COR",
        timestamp=TEST_TIME,
    )

    assert result.action == "nack"
    assert result.response["reason"] == "IDPK_EQUALS_MSGID"
    assert result.response["code"] == 422

#El cycleId rechazado debe incluirse dentro del NACK.
def test_nack_keeps_cycle_id():
    payload = {
        "idpk": "550e8400-e29b-41d4-a716-446655440000",
        "msgId": "550e8400-e29b-41d4-a716-446655440001",
        "type": "status-statement",
        "timestamp": "2026-09-15T20:00:00Z",
        "sender": "central",
        "cycleId": "cycle-9431",
        "data": {},
    }

    result = plan_protocol_response(
        payload,
        city_id="COR",
        timestamp=TEST_TIME,
    )

    assert result.action == "nack"
    assert result.response["data"]["cycleId"] == "cycle-9431"

#Un ACK recibido no debe generar otra respuesta.
def test_ack_message_is_ignored():
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

    result = plan_protocol_response(payload, city_id="COR")

    assert result.action == "ignore"
    assert result.response is None

#Sin msgId no existe un mensaje al cual responder.
def test_missing_msgid_is_discarded():
    payload = {
        "idpk": "550e8400-e29b-41d4-a716-446655440000",
        "type": "status-statement",
        "timestamp": "2026-09-15T20:00:00Z",
        "sender": "central",
        "data": {},
    }

    result = plan_protocol_response(payload, city_id="COR")

    assert result.action == "discard"
    assert result.response is None