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
    # Un mensaje recibido de tipo ack/nack/error nunca debe iniciar otra respuesta.
    if result.action not in {"ack", "nack"}:
        return False
    if result.response is None or result.response.get("type") != result.action:
        raise ValueError("ACK/NACK planificado sin respuesta del mismo tipo")

    await publish_protocol_message(
        exchange=exchange,
        routing_key=routing_key,
        payload=result.response,
        user_id=user_id,
    )

    return True
