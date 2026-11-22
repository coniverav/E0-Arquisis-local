"""Relays AD8 para RabbitMQ y Step Functions, desactivados por defecto."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import ssl
from typing import Any, Literal
from uuid import uuid4

import aio_pika
import httpx
from pamqp.commands import Basic

from protocol.outbound import select_outbound_destination
from protocol.publisher import build_amqp_message, build_city_user_id


logger = logging.getLogger("connector.outbox_relay")

CLAIM_URL = os.getenv(
    "MASTER_OUTBOX_RELAY_CLAIM_URL",
    "http://master:8000/internal/outbox-relay/claims",
)
RESULT_URL = os.getenv(
    "MASTER_OUTBOX_RELAY_RESULT_URL",
    "http://master:8000/internal/outbox-relay",
)
POLL_SECONDS = float(os.getenv("E3_OUTBOX_RELAY_POLL_SECONDS", "1"))
LEASE_SECONDS = int(os.getenv("E3_OUTBOX_RELAY_LEASE_SECONDS", "30"))
BATCH_SIZE = int(os.getenv("E3_OUTBOX_RELAY_BATCH_SIZE", "20"))
PUBLISH_TIMEOUT_SECONDS = float(os.getenv("E3_OUTBOX_PUBLISH_TIMEOUT_SECONDS", "10"))

class SagaIdentityMismatch(RuntimeError):
    """El nombre existe en AWS, pero no corresponde a la intención local."""


def _enabled(name: str) -> bool:
    return os.getenv(name, "false").strip().lower() in {"1", "true", "yes", "on"}


def broker_relay_enabled() -> bool:
    return _enabled("E3_BROKER_OUTBOX_RELAY_ENABLED")


def saga_relay_enabled() -> bool:
    return _enabled("E3_SAGA_OUTBOX_RELAY_ENABLED")


def _worker_id(kind: str) -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{kind.lower()}:{uuid4()}"


def _retry_seconds(item: dict[str, Any]) -> int:
    attempt = item.get("attemptCount", 1)
    attempt = attempt if isinstance(attempt, int) and attempt > 0 else 1
    return min(30, 2 ** min(attempt, 4))


def _aws_error_code(exc: Exception) -> str | None:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return None
    error = response.get("Error")
    return error.get("Code") if isinstance(error, dict) else None


def _safe_error(exc: Exception) -> str:
    return (_aws_error_code(exc) or type(exc).__name__)[:2000]


async def claim_items(
    client: httpx.AsyncClient,
    *,
    kind: Literal["BROKER", "STEP_FUNCTIONS"],
    worker_id: str,
) -> list[dict[str, Any]]:
    response = await client.post(CLAIM_URL, json={
        "workerId": worker_id,
        "deliveryKind": kind,
        "limit": BATCH_SIZE,
        "leaseSeconds": LEASE_SECONDS,
    })
    response.raise_for_status()
    items = response.json().get("items")
    if not isinstance(items, list) or any(
        not isinstance(item, dict) or item.get("deliveryKind") != kind
        for item in items
    ):
        raise ValueError("MASTER_RETURNED_INVALID_OUTBOX_CLAIMS")
    return items


async def finish_item(
    client: httpx.AsyncClient,
    item: dict[str, Any],
    *,
    outcome: Literal["PUBLISHED", "STARTED", "FAILED"],
    error: str | None = None,
    execution_arn: str | None = None,
) -> bool:
    body: dict[str, Any] = {
        "claimToken": item["claimToken"],
        "outcome": outcome,
    }
    if error is not None:
        body["error"] = error[:2000]
        body["retryAfterSeconds"] = _retry_seconds(item)
    if execution_arn is not None:
        body["executionArn"] = execution_arn
    response = await client.post(f"{RESULT_URL}/{item['id']}/result", json=body)
    if response.status_code == 409:
        return False
    response.raise_for_status()
    return True


async def publish_broker_item(
    item: dict[str, Any],
    *,
    directed_exchange,
    market_exchange,
    city_id: str,
    directed_exchange_name: str,
    market_exchange_name: str,
    central_routing_key: str,
) -> None:
    """Publica exactamente el envelope guardado y exige Basic.Ack."""

    exchange_name, routing_key = select_outbound_destination(
        item,
        city_id=city_id,
        directed_exchange=directed_exchange_name,
        market_exchange=market_exchange_name,
        central_routing_key=central_routing_key,
    )
    exchange = market_exchange if exchange_name == market_exchange_name else directed_exchange
    message = build_amqp_message(item["payload"], build_city_user_id(city_id))
    confirmation = await exchange.publish(
        message,
        routing_key=routing_key,
        mandatory=True,
        timeout=PUBLISH_TIMEOUT_SECONDS,
    )
    if not isinstance(confirmation, Basic.Ack):
        raise RuntimeError("BROKER_PUBLISH_NOT_CONFIRMED")


def _base_machine_arn(value: str) -> str:
    prefix, separator, resource = value.partition(":stateMachine:")
    if not separator or not resource:
        raise ValueError("INVALID_STATE_MACHINE_ARN")
    return f"{prefix}:stateMachine:{resource.split(':', 1)[0]}"


def _execution_arn(machine_arn: str, execution_name: str) -> str:
    base = _base_machine_arn(machine_arn)
    return base.replace(":stateMachine:", ":execution:") + f":{execution_name}"


def _stepfunctions_client():
    import boto3
    from botocore.config import Config

    return boto3.client(
        "stepfunctions",
        config=Config(
            connect_timeout=3,
            read_timeout=5,
            retries={"mode": "standard", "max_attempts": 3},
        ),
    )


def start_execution_once(item: dict[str, Any], *, stepfunctions=None) -> str:
    """Inicia con nombre=idpk o corrobora el replay ya existente."""

    machine_arn = item.get("stateMachineArn")
    name = item.get("executionName")
    serialized = item.get("executionInput")
    if (
        not isinstance(machine_arn, str)
        or not isinstance(name, str)
        or name != item.get("idpk")
        or not isinstance(serialized, str)
    ):
        raise ValueError("INVALID_SAGA_OUTBOX_INTENT")
    decoded = json.loads(serialized)
    if not isinstance(decoded, dict) or decoded.get("idpk") != name:
        raise ValueError("INVALID_SAGA_EXECUTION_INPUT")

    client = stepfunctions or _stepfunctions_client()
    try:
        result = client.start_execution(
            stateMachineArn=machine_arn,
            name=name,
            input=serialized,
        )
    except Exception as exc:
        if _aws_error_code(exc) != "ExecutionAlreadyExists":
            raise
        candidate = _execution_arn(machine_arn, name)
        known = client.describe_execution(executionArn=candidate)
        same_machine = _base_machine_arn(str(known.get("stateMachineArn", ""))) == _base_machine_arn(machine_arn)
        if known.get("name", name) != name or known.get("input") != serialized or not same_machine:
            raise SagaIdentityMismatch("EXECUTION_ALREADY_EXISTS_WITH_DIFFERENT_INPUT") from exc
        return candidate

    execution_arn = result.get("executionArn")
    if not isinstance(execution_arn, str) or ":execution:" not in execution_arn:
        raise RuntimeError("START_EXECUTION_WITHOUT_ARN")
    return execution_arn


async def broker_relay_forever(
    *,
    rabbitmq_url: str,
    city_id: str,
    directed_exchange_name: str,
    market_exchange_name: str,
    central_routing_key: str,
    http_timeout: float,
    reconnect_seconds: float,
) -> None:
    worker_id = _worker_id("BROKER")
    tls_context = ssl.create_default_context()
    while True:
        try:
            connection = await aio_pika.connect(
                rabbitmq_url,
                ssl_context=tls_context,
                timeout=10,
                client_properties={"connection_name": "energyshark-ad8-broker-relay"},
            )
            async with connection:
                channel = await connection.channel(
                    publisher_confirms=True,
                    on_return_raises=True,
                )
                directed = await channel.get_exchange(directed_exchange_name, ensure=False)
                market = await channel.get_exchange(market_exchange_name, ensure=False)
                async with httpx.AsyncClient(timeout=http_timeout) as client:
                    while True:
                        items = await claim_items(client, kind="BROKER", worker_id=worker_id)
                        for item in items:
                            try:
                                await publish_broker_item(
                                    item,
                                    directed_exchange=directed,
                                    market_exchange=market,
                                    city_id=city_id,
                                    directed_exchange_name=directed_exchange_name,
                                    market_exchange_name=market_exchange_name,
                                    central_routing_key=central_routing_key,
                                )
                            except Exception as exc:
                                logger.warning("Relay Broker falló row=%s error=%s", item.get("id"), _safe_error(exc))
                                try:
                                    await finish_item(client, item, outcome="FAILED", error=_safe_error(exc))
                                except Exception:
                                    logger.exception("No se pudo persistir fallo del relay Broker row=%s", item.get("id"))
                                if connection.is_closed or channel.is_closed:
                                    raise
                            else:
                                try:
                                    await finish_item(client, item, outcome="PUBLISHED")
                                except Exception:
                                    # El lease recuperará y republicará el mismo
                                    # envelope: entrega al menos una vez.
                                    logger.exception("Confirm Broker sin persistir row=%s", item.get("id"))
                        await asyncio.sleep(POLL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Relay Broker desconectado (%s)", _safe_error(exc))
            await asyncio.sleep(reconnect_seconds)


async def saga_relay_forever(*, http_timeout: float, reconnect_seconds: float) -> None:
    worker_id = _worker_id("STEP_FUNCTIONS")
    stepfunctions = _stepfunctions_client()
    while True:
        try:
            async with httpx.AsyncClient(timeout=http_timeout) as client:
                while True:
                    items = await claim_items(client, kind="STEP_FUNCTIONS", worker_id=worker_id)
                    for item in items:
                        try:
                            execution_arn = await asyncio.to_thread(
                                start_execution_once,
                                item,
                                stepfunctions=stepfunctions,
                            )
                        except Exception as exc:
                            logger.warning("Relay saga falló row=%s error=%s", item.get("id"), _safe_error(exc))
                            try:
                                await finish_item(client, item, outcome="FAILED", error=_safe_error(exc))
                            except Exception:
                                logger.exception("No se pudo persistir fallo del relay saga row=%s", item.get("id"))
                        else:
                            try:
                                await finish_item(
                                    client,
                                    item,
                                    outcome="STARTED",
                                    execution_arn=execution_arn,
                                )
                            except Exception:
                                # Un nuevo claim repetirá StartExecution con el
                                # mismo nombre/input y corroborará la ejecución.
                                logger.exception("StartExecution sin persistir row=%s", item.get("id"))
                    await asyncio.sleep(POLL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Relay saga interrumpido (%s)", _safe_error(exc))
            await asyncio.sleep(reconnect_seconds)
