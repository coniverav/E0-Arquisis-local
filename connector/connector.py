import asyncio
import logging
import os
from pathlib import Path
import ssl

from protocol.dispatch import dispatch_protocol_response
from protocol.handler import plan_protocol_response
from protocol.publisher import build_city_user_id
from protocol.intake import DiscardMessage, decode_incoming_payload
from protocol.routing import select_master_url

import aio_pika
import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("connector")

RABBITMQ_URL = os.environ["RABBITMQ_URL"]
RABBITMQ_QUEUE = os.environ["RABBITMQ_QUEUE"]
MASTER_URL = os.getenv("MASTER_URL", "http://master:8000/internal/events")
MASTER_ERROR_URL = os.getenv("MASTER_ERROR_URL", "http://master:8000/internal/protocol/errors",)
MASTER_AUDIT_URL = os.getenv("MASTER_AUDIT_URL", "http://master:8000/internal/audit/inbound",)
CONNECTOR_RETRY_SECONDS = float(os.getenv("CONNECTOR_RETRY_SECONDS", "5"))
HTTP_RETRY_SECONDS = float(os.getenv("HTTP_RETRY_SECONDS", "2"))
HTTP_TIMEOUT_SECONDS = float(os.getenv("HTTP_TIMEOUT_SECONDS", "10"))

CITY_ID = os.environ["CITY_ID"]
RABBITMQ_OUTBOUND_EXCHANGE = os.environ["RABBITMQ_OUTBOUND_EXCHANGE"]
RABBITMQ_CENTRAL_ROUTING_KEY = os.environ["RABBITMQ_CENTRAL_ROUTING_KEY"]

HEARTBEAT = Path("/tmp/energyshark_connector_heartbeat")


async def heartbeat() -> None:
    """Mantiene evidencia de que el proceso principal sigue vivo para HEALTHCHECK."""
    while True:
        HEARTBEAT.touch()
        await asyncio.sleep(10)


async def forward_to_master(
    client: httpx.AsyncClient,
    payload: dict,
    url: str = MASTER_URL,
) -> str:
    """Devuelve 'ack', 'drop' o 'retry' según la respuesta HTTP de master."""
    try:
        response = await client.post(url, json=payload)
    except httpx.HTTPError as exc:
        logger.warning("master no disponible: %s", exc)
        return "retry"

    if 200 <= response.status_code < 300:
        return "ack"

    # Un 4xx indica que el JSON recibido no cumple el contrato esperado.
    # Reencolarlo infinitamente solo bloquearía la cola.
    if 400 <= response.status_code < 500:
        logger.error(
            "master rechazó un evento (%s): %s",
            response.status_code,
            response.text[:500],
        )
        return "drop"

    logger.warning("master respondió %s; se reintentará", response.status_code)
    return "retry"

#Construye la evidencia que se enviará al master para un mensaje descartado antes de entrar al procesamiento normal del protocolo.
def build_discard_audit_payload(
    body: bytes,
    exc: DiscardMessage,
) -> dict:
    parsed_payload = exc.payload

    return {
        "status": "DISCARDED",
        "msgId": (
            str(parsed_payload.get("msgId"))
            if parsed_payload is not None
            and parsed_payload.get("msgId") is not None
            else None
        ),
        "idpk": (
            str(parsed_payload.get("idpk"))
            if parsed_payload is not None
            and parsed_payload.get("idpk") is not None
            else None
        ),
        "type": (
            parsed_payload.get("type")
            if parsed_payload is not None
            else None
        ),
        "cycleId": (
            parsed_payload.get("cycleId")
            if parsed_payload is not None
            else None
        ),
        "sender": (
            parsed_payload.get("sender")
            if parsed_payload is not None
            else None
        ),
        "payload": parsed_payload,
        "rawPayload": body.decode(
            "utf-8",
            errors="replace",
        ),
        "reason": str(exc),
    }

#Construye la evidencia del mensaje original rechazado mediante un NACK del protocolo.
def build_nack_audit_payload(
    payload: dict,
    response: dict,
) -> dict:
    data = response.get("data", {})

    return {
        "status": "NACKED",
        "msgId": (
            str(payload.get("msgId"))
            if payload.get("msgId") is not None
            else None
        ),
        "idpk": (
            str(payload.get("idpk"))
            if payload.get("idpk") is not None
            else None
        ),
        "type": payload.get("type"),
        "cycleId": payload.get("cycleId"),
        "sender": payload.get("sender"),
        "payload": payload,
        "reasonCode": response.get("reason"),
        "reason": data.get(
            "message",
            "Mensaje rechazado por el protocolo",
        ),
    }

async def consume_forever() -> None:
    # create_default_context() usa las CA públicas del sistema y MANTIENE la verificación TLS.
    tls_context = ssl.create_default_context()

    while True:
        try:
            logger.info("Conectando a RabbitMQ; cola=%s", RABBITMQ_QUEUE)
            # Usamos conexión normal + bucle de retry explícito. Así evitamos que una
            # reconexión robusta intente restaurar/declarar topología que este usuario
            # restringido no tiene permiso de modificar.
            connection = await aio_pika.connect(
                RABBITMQ_URL,
                ssl_context=tls_context,
                timeout=10,
                client_properties={"connection_name": "energyshark-observer-connector"},
            )

            async with connection:
                channel = await connection.channel()
                # Evita tomar demasiados eventos si master está temporalmente lento.
                await channel.set_qos(prefetch_count=20)

                # IMPORTANTE: ensure=False evita queue.declare (incluso passive).
                # Las credenciales observer solo pueden CONSUMIR la topología del servidor.
                queue = await channel.get_queue(RABBITMQ_QUEUE, ensure=False)

                #Se obtiene el exchange existente sin declarar topología nueva.
                exchange = await channel.get_exchange(
                    RABBITMQ_OUTBOUND_EXCHANGE,
                    ensure=False,
                )

                city_user_id = build_city_user_id(
                    CITY_ID
                )

                logger.info("Conectado. Esperando eventos en %s", RABBITMQ_QUEUE)

                async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
                    async with queue.iterator() as iterator:
                        async for message in iterator:
                            try:
                                payload = decode_incoming_payload(message.body)
                            except DiscardMessage as exc:
                                logger.error(
                                    "Mensaje descartado sin respuesta de protocolo: %s",
                                    exc,
                                )

                                audit_payload = build_discard_audit_payload(
                                    message.body,
                                    exc,
                                )

                                audit_result = await forward_to_master(
                                    client,
                                    audit_payload,
                                    url=MASTER_AUDIT_URL,
                                )

                                if audit_result == "ack":
                                    #La evidencia quedó persistida. El mensaje inválido puede eliminarse definitivamente de la cola.
                                    await message.reject(requeue=False)

                                elif audit_result == "retry":
                                    #Si master/DB no están disponibles, no perdemos la evidencia del descarte.
                                    await message.reject(requeue=True)
                                    await asyncio.sleep(
                                        HTTP_RETRY_SECONDS
                                    )

                                else:
                                    #Un error del estilo 400 desde el endpoint de auditoría indica un problema permanente con el registro enviado.
                                    logger.error(
                                        "master rechazó la auditoría del mensaje descartado"
                                    )
                                    await message.reject(requeue=False)

                                continue

                            #El mensaje sobrevivió al intake. Ahora se parsea y valida a nivel de protocolo.
                            handling = plan_protocol_response(
                                payload,
                                city_id=CITY_ID,
                            )

                            if handling.action == "nack":
                                audit_payload = build_nack_audit_payload(
                                    payload,
                                    handling.response,
                                )

                                audit_result = await forward_to_master(
                                    client,
                                    audit_payload,
                                    url=MASTER_AUDIT_URL,
                                )

                                if audit_result == "retry":
                                    #Si no pudimos persistir la evidencia, el mensaje vuelve a la cola.
                                    await message.reject(
                                        requeue=True
                                    )
                                    await asyncio.sleep(
                                        HTTP_RETRY_SECONDS
                                    )
                                    continue

                                if audit_result == "drop":
                                    #La auditoría fue rechazada de forma permanente.
                                    logger.error(
                                        "master rechazó la auditoría del NACK"
                                    )
                                    await message.reject(
                                        requeue=False
                                    )
                                    continue

                                await dispatch_protocol_response(
                                    result=handling,
                                    exchange=exchange,
                                    routing_key=(
                                        RABBITMQ_CENTRAL_ROUTING_KEY
                                    ),
                                    user_id=city_user_id,
                                )

                                #El rechazo quedó auditado y el NACK fue publicado correctamente.
                                await message.ack()
                                continue

                            master_url = select_master_url(
                                payload,
                                default_url=MASTER_URL,
                                error_url=MASTER_ERROR_URL,
                            )

                            result = await forward_to_master(
                                client,
                                payload,
                                url=master_url,
                            )

                            if result == "ack":
                                # ACK solo después de persistir con éxito en master/PostgreSQL.
                                await message.ack()
                            elif result == "drop":
                                await message.reject(requeue=False)
                            else:
                                # Si master/DB fallan, el evento vuelve a RabbitMQ.
                                await message.reject(requeue=True)
                                await asyncio.sleep(HTTP_RETRY_SECONDS)

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # El proceso NO muere si RabbitMQ pierde conexión.
            logger.warning(
                "Conexión/consumo interrumpido (%s). Reintentando en %.1fs...",
                exc,
                CONNECTOR_RETRY_SECONDS,
            )
            await asyncio.sleep(CONNECTOR_RETRY_SECONDS)


async def main() -> None:
    heartbeat_task = asyncio.create_task(heartbeat())
    try:
        await consume_forever()
    finally:
        heartbeat_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
