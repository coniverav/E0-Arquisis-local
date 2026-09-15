from protocol.parser import parse_envelope

#Verifica el parseo de un mensaje v2 emitido por la central.
def test_parse_v2_envelope():
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
                "generationCost": 200
            },
            "validUntil": "2026-09-13T22:00:00Z"
        }
    }

    message = parse_envelope(payload)

    assert str(message.idpk) == payload["idpk"]
    assert str(message.msg_id) == payload["msgId"]
    assert message.type == "status-statement"
    assert message.sender == "central"
    assert message.city_id is None
    assert message.cycle_id == "cycle-1"

#Verifica el parseo de un mensaje v2 emitido por una ciudad.
def test_parse_city_envelope():
    payload = {
        "idpk": "550e8400-e29b-41d4-a716-446655440000",
        "msgId": "550e8400-e29b-41d4-a716-446655440001",
        "type": "request",
        "timestamp": "2026-09-13T20:00:00Z",
        "cityId": "COR",
        "data": {
            "ask": "status-statement"
        }
    }

    message = parse_envelope(payload)

    assert message.city_id == "COR"
    assert message.sender is None