import json
import aio_pika

#Construye el user_id AMQP asociado a una ciudad.
def build_city_user_id(city_id: str) -> str:
    return f"city.{city_id}"

#Construye un mensaje AMQP listo para publicar.
def build_amqp_message(payload: dict, user_id: str) -> aio_pika.Message:
    return aio_pika.Message(
        body=json.dumps(payload).encode("utf-8"),
        content_type="application/json",
        user_id=user_id,
    )

#Publica un mensaje del protocolo en el destino indicado.
async def publish_protocol_message(
    exchange,
    routing_key: str,
    payload: dict,
    user_id: str,
) -> None:
    message = build_amqp_message(payload, user_id)

    await exchange.publish(
        message,
        routing_key=routing_key,
    )