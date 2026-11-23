from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Mapping

from postgres_store import PostgresStore, database_dsn
from wallet_client import WalletClient, WalletConfig, WalletResult
from seller_reservation import verify_seller_reservation, verify_seller_rejection
from seller_proposal import prepare_seller_proposal
from seller_bid_accepted import verify_persisted_bid_accepted


logger = logging.getLogger()
logger.setLevel(logging.INFO)


class SagaTaskNotImplemented(RuntimeError):
    """Evita ejecutar parcialmente una tarea fuera de E3-022/E3-023."""


_secret_cache: dict[str, dict[str, Any]] = {}


def _correlation(event: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: event[key]
        for key in ("cycleId", "swapId", "idpk", "correlationIdpk", "sagaExecutionId")
        if event.get(key) is not None
    }


def _secret(arn: str) -> dict[str, Any]:
    if arn in _secret_cache:
        return _secret_cache[arn]
    import boto3

    response = boto3.client("secretsmanager").get_secret_value(SecretId=arn)
    raw = response.get("SecretString")
    if not isinstance(raw, str):
        raise RuntimeError("Secrets Manager no devolvió SecretString")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise RuntimeError("El secret no contiene un objeto JSON")
    _secret_cache[arn] = value
    return value


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} no está configurado")
    return value


def _parse_instant(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} es obligatorio")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"{field} requiere zona horaria")
    return parsed.astimezone(timezone.utc)


def _remaining_seconds(context: Any) -> float:
    getter = getattr(context, "get_remaining_time_in_millis", None)
    return max(0.1, (getter() / 1000.0) - 1.0) if getter else 20.0


def _build_store() -> PostgresStore:
    secret = _secret(_required_env("DATABASE_SECRET_ARN"))
    return PostgresStore(
        database_dsn(secret),
        city_id=os.environ.get("CITY_ID", "KLD"),
    )


def _build_wallet(store: PostgresStore) -> WalletClient:
    secret = _secret(_required_env("WALLET_SECRET_ARN"))
    return WalletClient(WalletConfig.from_secret(secret), store)


def _register_callback(event: Mapping[str, Any], store: PostgresStore) -> dict[str, Any]:
    wait_type = str(event.get("waitType") or (
        "PROPOSAL" if event.get("operation") == "PROPOSE" else "TERMINAL"
    )).upper()
    return store.register_task_token(
        saga_execution_id=event.get("sagaExecutionId"),
        task_token=str(event.get("taskToken") or ""),
        wait_type=wait_type,
        timeout_at=_parse_instant(event.get("timeoutAt"), "timeoutAt"),
        correlation_idpk=event.get("correlationIdpk") or event.get("idpk"),
        swap_id=event.get("swapId"),
    )


def _wallet_result(result: WalletResult) -> dict[str, Any]:
    return {
        "statusCode": result.status_code,
        "acceptedForProcessing": result.accepted_for_processing,
        "idempotencyKey": result.idempotency_key,
        "wallet": result.data,
    }


def _known_outcome(result: WalletResult) -> dict[str, Any] | None:
    if result.status_code != 200:
        return None
    status = str(result.data.get("status") or "").upper()
    outcome = {
        "PROPOSED": "PROPOSED",
        "SETTLED": "SETTLED",
        "REJECTED": "REJECTED",
        "EXPIRED": "EXPIRED",
        "CANCELLED": "CANCELLED",
    }.get(status)
    if outcome is None:
        return None
    response = {"outcome": outcome}
    for source in ("swapId", "expiresAt"):
        if result.data.get(source) is not None:
            response[source] = result.data[source]
    return response


def _complete_known_callback(
    *,
    task_token: str,
    outcome: dict[str, Any],
    store: PostgresStore,
) -> None:
    import boto3

    client = boto3.client("stepfunctions")
    try:
        client.send_task_success(
            taskToken=task_token,
            output=json.dumps(outcome, separators=(",", ":")),
        )
    except client.exceptions.TaskDoesNotExist:
        # Un evento pudo ganar la carrera. Sólo es benigno si ya quedó durable.
        if store.task_token_status(task_token) != "SUCCEEDED":
            raise
        return
    store.mark_callback_succeeded(task_token)


def _callback_wallet_task(
    task: str,
    event: Mapping[str, Any],
    context: Any,
    store: PostgresStore,
    wallet: WalletClient,
) -> dict[str, Any]:
    # La transacción termina antes de reservar cuota o abrir la conexión HTTP.
    token = _register_callback(event, store)
    if token["status"] != "WAITING":
        return {"callbackAlreadyCompleted": True}

    key = event.get("idempotencyKey")
    if not isinstance(key, str) or not key:
        raise ValueError("idempotencyKey estable es obligatoria")
    deadline = _remaining_seconds(context)
    if task == "seller-proposal":
        swap = event.get("swap")
        if not isinstance(swap, dict):
            raise ValueError("swap es obligatorio")
        # E3-040: el callback ya está persistido. Congelar el cuerpo Wallet
        # bajo lock antes del HTTP evita expiresAt nuevo en cada retry.
        proposal_body = prepare_seller_proposal(
            store,
            saga_execution_id=event.get("sagaExecutionId"),
            idempotency_key=key,
            swap=swap,
        )
        result = wallet.propose(
            idempotency_key=key,
            swap=proposal_body,
            deadline_seconds=deadline,
        )
    elif task == "buyer-acceptance":
        swap_id = event.get("swapId")
        if not isinstance(swap_id, str) or not swap_id:
            raise ValueError("swapId es obligatorio")
        result = wallet.accept(
            swap_id=swap_id,
            idempotency_key=key,
            deadline_seconds=deadline,
        )
    else:
        raise SagaTaskNotImplemented(task)

    known = _known_outcome(result)
    if known is not None:
        _complete_known_callback(
            task_token=str(event["taskToken"]),
            outcome=known,
            store=store,
        )
    return _wallet_result(result)


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    task = _required_env("SAGA_TASK")
    logger.info(json.dumps({
        "event": "saga_task_invoked",
        "task": task,
        "requestId": getattr(context, "aws_request_id", None),
        **_correlation(event),
    }, sort_keys=True))

    supported = {
        "register-callback",
        "reserve-seller",
        "reject-seller-bid",
        "seller-proposal",
        "publish-bid-accepted",
        "buyer-acceptance",
        "reconcile-seller-swap",
        "reconcile-buyer-swap",
        "cancel-seller-swap",
    }
    if task not in supported:
        raise SagaTaskNotImplemented(
            f"La tarea {task} permanece desactivada hasta su tarjeta funcional"
        )

    store = _build_store()
    if task == "reserve-seller":
        return verify_seller_reservation(
            store,
            bid_idpk=event.get("bidIdpk"),
            cycle_id=event.get("cycleId"),
            offer_id=event.get("offerId"),
            quantity=event.get("quantity"),
            saga_execution_id=event.get("sagaExecutionId"),
        )
    if task == "reject-seller-bid":
        return verify_seller_rejection(
            store, bid_idpk=event.get("bidIdpk"), reason=event.get("reason")
        )
    if task == "register-callback":
        return {"token": _register_callback(event, store)}
    if task == "publish-bid-accepted":
        return verify_persisted_bid_accepted(
            store,
            bid_idpk=event.get("bidIdpk"),
            cycle_id=event.get("cycleId"),
            swap_id=event.get("swapId"),
            expires_at=event.get("expiresAt"),
            saga_execution_id=event.get("sagaExecutionId"),
        )
    if task in {"seller-proposal", "buyer-acceptance"}:
        return _callback_wallet_task(
            task, event, context, store, _build_wallet(store)
        )
    if task in {"reconcile-seller-swap", "reconcile-buyer-swap"}:
        swap_id = event.get("swapId")
        if not isinstance(swap_id, str) or not swap_id:
            raise ValueError("swapId es obligatorio")
        return _wallet_result(_build_wallet(store).get_swap(
            swap_id=swap_id,
            deadline_seconds=_remaining_seconds(context),
        ))
    if task == "cancel-seller-swap":
        swap_id = event.get("swapId")
        key = event.get("idempotencyKey")
        if not isinstance(swap_id, str) or not isinstance(key, str) or not key:
            raise ValueError("swapId e idempotencyKey son obligatorios")
        return _wallet_result(_build_wallet(store).cancel(
            swap_id=swap_id,
            idempotency_key=key,
            deadline_seconds=_remaining_seconds(context),
        ))
    raise AssertionError("Tarea soportada sin despacho")
