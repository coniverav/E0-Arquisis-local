import os
import ssl
import json
import time
import threading
from datetime import datetime, timezone

import pika
import requests

# --------------------------------------------------------------------------
# Credenciales y configuración: vienen de variables de entorno (.env).
# NO las escribas directamente en este archivo.
# --------------------------------------------------------------------------
RABBITMQ_HOST = os.getenv("RABBITMQ_HOST", "broker.iic2173.org")
RABBITMQ_PORT = int(os.getenv("RABBITMQ_PORT", "5671"))
RABBITMQ_USER = os.getenv("RABBITMQ_USER")  # <-- tu usuario del broker (lo entregan por Canvas)
RABBITMQ_PASS = os.getenv("RABBITMQ_PASS")  # <-- tu contraseña del broker (la entregan por Canvas)
RABBITMQ_VHOST = os.getenv("RABBITMQ_VHOST", "energy")  # <-- virtual host que aparece en tus credenciales
QUEUE_NAME = os.getenv("QUEUE_NAME")        # <-- ej: "observer.23.q" (con tu número de usuario)

MASTER_URL = os.getenv("MASTER_URL", "http://master:8000")

HEARTBEAT_FILE = "/tmp/connector_alive"


def touch_heartbeat():
    """
    Escribe un archivo con la hora actual cada cierto tiempo.
    El HEALTHCHECK del Dockerfile revisa que este archivo esté 'fresco'
    para saber si el connector sigue vivo (RNF7).
    """
    while True:
        with open(HEARTBEAT_FILE, "w") as f:
            f.write(str(time.time()))
        time.sleep(15)


def send_to_master(body: dict):
    """Envía el evento recibido al servicio master vía HTTP POST."""
    payload = {
        "idpk": body["idpk"],
        "type": body["type"],
        "packageBody": body["packageBody"],
        "receivedAt": datetime.now(timezone.utc).isoformat(),
    }
    resp = requests.post(f"{MASTER_URL}/events", json=payload, timeout=10)
    resp.raise_for_status()


def on_message(channel, method, properties, body):
    """ 
    Callback que se ejecuta cada vez que llega un mensaje a la cola. 
    Llama a send_to_master() para guardarlo en master y 
    si hay error, guarda el mensaje en la cola para reintentar más tarde.
    """
    try:
        data = json.loads(body.decode("utf-8"))
        send_to_master(data)
        channel.basic_ack(delivery_tag=method.delivery_tag)
        print(f"[connector] evento {data.get('idpk')} guardado en master")
    except Exception as e:
        # Si algo falla (ej: master caído), NO confirmamos el mensaje,
        # así RabbitMQ nos lo vuelve a entregar más tarde.
        print(f"[connector] error procesando mensaje: {e}")
        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=True)


def connect_and_consume():
    """
    Se conecta a RabbitMQ y empieza a escuchar. Si la conexión se cae,
    el loop de afuera (main) la vuelve a levantar sola (RNF1).
    """
    credentials = pika.PlainCredentials(RABBITMQ_USER, RABBITMQ_PASS)

    ssl_context = ssl.create_default_context()
    ssl_options = pika.SSLOptions(ssl_context, RABBITMQ_HOST)

    parameters = pika.ConnectionParameters(
        host=RABBITMQ_HOST,
        port=RABBITMQ_PORT,
        virtual_host=RABBITMQ_VHOST,
        credentials=credentials,
        ssl_options=ssl_options,
        heartbeat=30,
        blocked_connection_timeout=30,
    )

    connection = pika.BlockingConnection(parameters)
    channel = connection.channel()
    channel.basic_qos(prefetch_count=10)
    channel.basic_consume(queue=QUEUE_NAME, on_message_callback=on_message)

    print(f"[connector] conectado, escuchando cola {QUEUE_NAME}")
    channel.start_consuming()


def main():
    # Crea un deamon thread que actualiza el heartbeat cada 15s para que el HEALTHCHECK del Dockerfile funcione
    threading.Thread(target=touch_heartbeat, daemon=True).start()

    # Flujo que evita que el connector se caiga si la conexión a RabbitMQ se pierde (RNF1)
    # > Lo reintenta automáticamente cada 5 segundos hasta que vuelva a estar disponible:)
    while True:
        try:
            connect_and_consume()
        except Exception as e:
            print(f"[connector] conexión perdida ({e}), reintentando en 5s...")
            time.sleep(5)


if __name__ == "__main__":
    main()
