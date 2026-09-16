import json

#Mensaje que debe descartarse sin generar una respuesta de protocolo.
class DiscardMessage(ValueError):
    pass

#Decodifica el body recibido desde RabbitMQ.
#Solo clasifica los casos que deben descartarse sin respuesta. Mensajes no parseables y mensajes sin msgId.
def decode_incoming_payload(body: bytes) -> dict:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DiscardMessage("El mensaje no es JSON válido") from exc

    #Un envelope del protocolo debe ser un objeto JSON.
    if not isinstance(payload, dict):
        raise DiscardMessage("El mensaje JSON no es un objeto")

    #Sin msgId no existe un mensaje válido al cual responder.
    if "msgId" not in payload:
        raise DiscardMessage("El mensaje no contiene msgId")

    return payload