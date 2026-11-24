"""Proceso de relay aislado para el outbox del broker.

El connector normal mezcla consumidor y relays. Este entrypoint arranca solo el
relay de broker, que es lo que E3-079 necesita para simular la caída del
proceso sin arrastrar la conexión de consumo de la ciudad.
"""

import asyncio
import logging
import os

from outbox_relay import broker_relay_forever


async def main() -> None:
    await broker_relay_forever(
        rabbitmq_url=os.environ["RABBITMQ_URL"],
        city_id=os.environ["CITY_ID"],
        directed_exchange_name=os.environ["RABBITMQ_OUTBOUND_EXCHANGE"],
        market_exchange_name=os.environ.get("RABBITMQ_MARKET_EXCHANGE", "market"),
        central_routing_key=os.environ.get("RABBITMQ_CENTRAL_ROUTING_KEY", "central"),
        http_timeout=float(os.environ.get("HTTP_TIMEOUT_SECONDS", "10")),
        reconnect_seconds=float(os.environ.get("CONNECTOR_RETRY_SECONDS", "5")),
    )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    asyncio.run(main())
