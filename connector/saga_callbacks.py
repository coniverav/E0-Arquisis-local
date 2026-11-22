"""Entrega callbacks de Step Functions sin registrar task tokens."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

import httpx


logger = logging.getLogger("connector.saga_callbacks")

TRANSIENT_AWS_ERRORS = {
    "InternalError",
    "KmsThrottlingException",
    "RequestTimeout",
    "RequestTimeoutException",
    "ServiceUnavailable",
    "ThrottlingException",
    "TooManyRequestsException",
}
ALREADY_DELIVERED_ERRORS = {"TaskDoesNotExist"}
PERMANENT_AWS_ERRORS = {"InvalidToken", "TaskTimedOut"}


def callbacks_enabled() -> bool:
    return os.getenv("E3_SAGA_CALLBACKS_ENABLED", "false").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _aws_error_code(exc: Exception) -> str | None:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return None
    error = response.get("Error")
    return error.get("Code") if isinstance(error, dict) else None


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


async def dispatch_stepfunctions_callback(
    instruction: dict[str, Any],
    *,
    stepfunctions=None,
) -> str:
    """Devuelve SUCCEEDED, FAILED o RETRY; nunca registra el token."""

    token = instruction.get("taskToken")
    mode = instruction.get("mode")
    if not isinstance(token, str) or not token or mode not in {"SUCCESS", "FAILURE"}:
        raise ValueError("Instrucción de callback inválida")
    client = stepfunctions or _stepfunctions_client()

    def send() -> None:
        if mode == "SUCCESS":
            output = instruction.get("output")
            if not isinstance(output, dict):
                raise ValueError("Output de callback inválido")
            client.send_task_success(
                taskToken=token,
                output=json.dumps(output, separators=(",", ":")),
            )
        else:
            error = str(instruction.get("error") or "WalletCallbackFailed")[:256]
            cause = str(instruction.get("cause") or "Callback Wallet no procesable")[:32768]
            client.send_task_failure(taskToken=token, error=error, cause=cause)

    try:
        await asyncio.to_thread(send)
        return "SUCCEEDED" if mode == "SUCCESS" else "FAILED"
    except Exception as exc:
        code = _aws_error_code(exc)
        if code in ALREADY_DELIVERED_ERRORS:
            return "SUCCEEDED"
        if code in PERMANENT_AWS_ERRORS:
            logger.error("Callback Step Functions no recuperable: %s", code)
            return "FAILED"
        if code in TRANSIENT_AWS_ERRORS or code is None:
            logger.warning("Callback Step Functions transitorio: %s", code or type(exc).__name__)
            return "RETRY"
        logger.warning("Callback Step Functions AWS no clasificado: %s", code)
        return "RETRY"


async def handle_wallet_saga_callback(
    client: httpx.AsyncClient,
    *,
    protocol_payload: dict[str, Any],
    master_payload: dict[str, Any] | None,
    result_url: str,
    stepfunctions=None,
) -> str:
    """Confirma AWS y luego persiste el resultado antes del ACK RabbitMQ."""

    if not callbacks_enabled():
        return "ack"
    instruction = (master_payload or {}).get("sagaCallback")
    if instruction is None:
        return "ack"
    if not isinstance(instruction, dict):
        return "retry"
    receipt = instruction.get("receipt")
    if not isinstance(receipt, str) or len(receipt) != 64:
        return "retry"

    try:
        delivery = await dispatch_stepfunctions_callback(
            instruction,
            stepfunctions=stepfunctions,
        )
    except ValueError:
        logger.exception("Master devolvió una instrucción de callback inválida")
        return "retry"
    if delivery == "RETRY":
        return "retry"

    try:
        response = await client.post(result_url, json={
            "receipt": receipt,
            "status": delivery,
            "callbackIdpk": protocol_payload.get("idpk"),
        })
    except httpx.HTTPError as exc:
        logger.warning("No se pudo persistir resultado de callback: %s", type(exc).__name__)
        return "retry"
    if not 200 <= response.status_code < 300:
        logger.warning("Master rechazó resultado de callback: HTTP %s", response.status_code)
        return "retry"
    return "ack"
