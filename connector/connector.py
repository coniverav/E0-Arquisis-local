import asyncio
import json
import logging
import os
from pathlib import Path
import ssl

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
CONNECTOR_RETRY_SECONDS = float(os.getenv("CONNECTOR_RETRY_SECONDS", "5"))
HTTP_RETRY_SECONDS = float(os.getenv("HTTP_RETRY_SECONDS", "2"))
HTTP_TIMEOUT_SECONDS = float(os.getenv("HTTP_TIMEOUT_SECONDS", "10"))

HEARTBEAT = Path("/tmp/energyshark_connector_heartbeat")


async def heartbeat() -> None:
    """Mantiene evidencia de que el proceso principal sigue vivo para HEALTHCHECK."""
    while True:
        HEARTBEAT.touch()
        await asyncio.sleep(10)


async def forward_to_master(client: httpx.AsyncClient, payload: dict) -> str:
    """Devuelve 'ack', 'drop' o 'retry' según la respuesta HTTP de master."""
    try:
        response = await client.post(MASTER_URL, json=payload)
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

                logger.info("Conectado. Esperando eventos en %s", RABBITMQ_QUEUE)

                async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
                    async with queue.iterator() as iterator:
                        async for message in iterator:
                            try:
                                payload = json.loads(message.body.decode("utf-8"))
                            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                                logger.error("Mensaje no es JSON válido; se descarta: %s", exc)
                                await message.reject(requeue=False)
                                continue

                            result = await forward_to_master(client, payload)

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
