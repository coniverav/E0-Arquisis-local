import os

#connector.py lee estas variables al momento de importarse.
os.environ.setdefault(
    "RABBITMQ_URL",
    "amqps://test:test@example.org/test",
)
os.environ.setdefault(
    "RABBITMQ_QUEUE",
    "test.queue",
)
os.environ.setdefault(
    "CITY_ID",
    "COR",
)
os.environ.setdefault(
    "RABBITMQ_OUTBOUND_EXCHANGE",
    "test.exchange",
)
os.environ.setdefault(
    "RABBITMQ_CENTRAL_ROUTING_KEY",
    "central.test",
)

from connector import (
    build_discard_audit_payload,
    build_nack_audit_payload,
)
from protocol.intake import DiscardMessage


#Un descarte sin msgId conserva toda la metadata que sí pudo recuperarse para que master pueda auditar el mensaje.
def test_build_discard_audit_payload_keeps_available_metadata():
    parsed_payload = {
        "idpk": "550e8400-e29b-41d4-a716-446655440000",
        "type": "status-statement",
        "cycleId": "cycle-test",
        "sender": "central",
    }

    error = DiscardMessage(
        "El mensaje no contiene msgId",
        payload=parsed_payload,
    )

    audit = build_discard_audit_payload(
        b'{"idpk":"550e8400-e29b-41d4-a716-446655440000"}',
        error,
    )

    assert audit["status"] == "DISCARDED"
    assert audit["msgId"] is None
    assert (
        audit["idpk"]
        == "550e8400-e29b-41d4-a716-446655440000"
    )
    assert audit["type"] == "status-statement"
    assert audit["cycleId"] == "cycle-test"
    assert audit["sender"] == "central"
    assert audit["payload"] == parsed_payload
    assert audit["reason"] == "El mensaje no contiene msgId"


#Un body que ni siquiera puede decodificarse conserva una representación legible en rawPayload.
def test_build_discard_audit_payload_keeps_invalid_raw_body():
    error = DiscardMessage(
        "El mensaje no es JSON válido"
    )

    audit = build_discard_audit_payload(
        b"\xff\xfe\xfa",
        error,
    )

    assert audit["status"] == "DISCARDED"
    assert audit["msgId"] is None
    assert audit["idpk"] is None
    assert audit["payload"] is None
    assert audit["rawPayload"] is not None
    assert audit["reason"] == "El mensaje no es JSON válido"


#El registro NACKED representa al mensaje original rechazado, mientras reasonCode/reason provienen del NACK generado.
def test_build_nack_audit_payload_keeps_original_message():
    original = {
        "idpk": "550e8400-e29b-41d4-a716-446655440000",
        "msgId": "550e8400-e29b-41d4-a716-446655440001",
        "type": "status-statement",
        "timestamp": "2026-09-20T20:00:00Z",
        "sender": "central",
        "cycleId": "cycle-test",
        "data": {},
    }

    nack = {
        "idpk": "550e8400-e29b-41d4-a716-446655440010",
        "msgId": "550e8400-e29b-41d4-a716-446655440011",
        "type": "nack",
        "reason": "MALFORMED_MESSAGE",
        "code": 422,
        "data": {
            "target": original["msgId"],
            "message": "Falta data.energy",
        },
    }

    audit = build_nack_audit_payload(
        original,
        nack,
    )

    assert audit["status"] == "NACKED"
    assert audit["msgId"] == original["msgId"]
    assert audit["idpk"] == original["idpk"]
    assert audit["type"] == "status-statement"
    assert audit["cycleId"] == "cycle-test"
    assert audit["payload"] == original
    assert audit["reasonCode"] == "MALFORMED_MESSAGE"
    assert audit["reason"] == "Falta data.energy"