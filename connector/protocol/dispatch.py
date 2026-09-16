from .handler import ProtocolHandlingResult
from .publisher import publish_protocol_message

#Publica la respuesta preparada por el handler.
#Retorna True si se publicó una respuesta y False si la acción no requiere enviar ACK/NACK.
async def dispatch_protocol_response(
    result: ProtocolHandlingResult,
    exchange,
    routing_key: str,
    user_id: str,
) -> bool:
    if result.response is None:
        return False

    await publish_protocol_message(
        exchange=exchange,
        routing_key=routing_key,
        payload=result.response,
        user_id=user_id,
    )

    return True