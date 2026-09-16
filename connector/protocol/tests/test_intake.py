import pytest

from protocol.intake import DiscardMessage, decode_incoming_payload

#Un JSON con msgId continúa hacia las siguientes capas del protocolo.
def test_accept_json_with_msgid():
    body = b'''
    {
        "msgId": "550e8400-e29b-41d4-a716-446655440001"
    }
    '''

    payload = decode_incoming_payload(body)

    assert payload["msgId"] == "550e8400-e29b-41d4-a716-446655440001"

#Un body que no puede parsearse como JSON debe descartarse.
def test_discard_invalid_json():
    body = b'{"msgId": "incompleto"'

    with pytest.raises(DiscardMessage):
        decode_incoming_payload(body)

#Un body que no puede decodificarse como UTF-8 debe descartarse.
def test_discard_invalid_utf8():
    body = b"\xff\xfe\xfa"

    with pytest.raises(DiscardMessage):
        decode_incoming_payload(body)

#Sin msgId no existe un mensaje válido al cual responder.
def test_discard_json_without_msgid():
    body = b'''
    {
        "idpk": "550e8400-e29b-41d4-a716-446655440000",
        "type": "status-statement"
    }
    '''

    with pytest.raises(DiscardMessage):
        decode_incoming_payload(body)

#El envelope debe ser un objeto JSON.
def test_discard_json_that_is_not_object():
    body = b'["mensaje", "invalido"]'

    with pytest.raises(DiscardMessage):
        decode_incoming_payload(body)