"""E2-027: observabilidad de los dos consumidores del connector.

Se interceptan únicamente ACK/REJECT del broker DESPUÉS de su ejecución.
No se alteran el flujo AMQP ni sus ACK/NACK de protocolo.
"""

import json
import logging
import re
import time

logger = logging.getLogger("connector.observability")

try:
    import newrelic.agent as nr_agent
except ImportError:
    nr_agent = None


def _safe(value):
    if isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9_.-]{1,80}", value):
        return value
    return None


def record_consumer_connection(consumer: str, state: str, error_type: str | None = None):
    fields = {"consumer": consumer, "state": state}
    if error_type is not None:
        fields["errorType"] = error_type
    logger.info("broker_connection %s", " ".join(f"{k}={v}" for k, v in fields.items()))
    if nr_agent is not None:
        try:
            nr_agent.record_custom_event("EnergySharkBrokerConnection", fields, application=nr_agent.application())
        except Exception:
            logger.debug("No fue posible registrar conexión en New Relic")


def record_consumer_heartbeat(consumer: str) -> None:
    """Señal periódica del consumidor conectado, incluso sin ofertas nuevas.

    No representa mensajes realmente procesados. Es útil para alertas de
    pérdida de señal sin confundir silencio del mercado con una caída.
    """
    if consumer not in {"city", "market"}:
        raise ValueError("consumer desconocido")
    fields = {"consumer": consumer}
    logger.info("broker_heartbeat consumer=%s", consumer)
    if nr_agent is not None:
        try:
            nr_agent.record_custom_event(
                "EnergySharkBrokerHeartbeat", fields,
                application=nr_agent.application(),
            )
        except Exception:
            logger.debug("No fue posible registrar latido de consumidor en New Relic")


class ObservedIncomingMessage:
    """Proxy transparente de entrega AMQP con auditoría de duración y resultado."""

    def __init__(self, original, consumer: str):
        self.original = original
        self.consumer = consumer
        self.started_at = time.perf_counter()
        self.fields = {}
        try:
            body = original.body
            if isinstance(body, bytes) and len(body) <= 65536:
                payload = json.loads(body)
                if isinstance(payload, dict):
                    for field in ("msgId", "idpk", "type", "cycleId"):
                        value = _safe(payload.get(field))
                        if value is not None:
                            self.fields[field] = value
        except (ValueError, TypeError, UnicodeError):
            pass

    def __getattr__(self, name):
        return getattr(self.original, name)

    def _record(self, outcome: str, requeue: bool | None = None):
        fields = {"consumer": self.consumer, "outcome": outcome,
                  "latencyMs": round((time.perf_counter() - self.started_at) * 1000, 2),
                  **self.fields}
        if requeue is not None:
            fields["requeue"] = requeue
        logger.info("broker_delivery %s", " ".join(f"{k}={v}" for k, v in fields.items()))
        if nr_agent is not None:
            try:
                nr_agent.record_custom_event("EnergySharkBrokerDelivery", fields, application=nr_agent.application())
            except Exception:
                logger.debug("No fue posible registrar entrega en New Relic")

    async def ack(self, *args, **kwargs):
        result = await self.original.ack(*args, **kwargs)
        self._record("broker_ack")
        return result

    async def reject(self, *args, **kwargs):
        result = await self.original.reject(*args, **kwargs)
        requeue = kwargs.get("requeue", args[0] if args else False)
        self._record("broker_reject", bool(requeue))
        return result
