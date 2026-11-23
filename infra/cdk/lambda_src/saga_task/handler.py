from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from postgres_store import PostgresStore, database_dsn
from wallet_client import WalletClient, WalletConfig, WalletResult
from seller_reservation import verify_seller_reservation, verify_seller_rejection
from seller_proposal import prepare_seller_proposal
from seller_bid_accepted import verify_persisted_bid_accepted
from seller_terminal import compensate_seller, finalize_seller, known_seller_swap_id, SellerTerminalInconsistent
from buyer_acceptance import (
    BuyerAcceptanceInconsistent, verify_buyer_acceptance_identity,
    prepare_buyer_acceptance, complete_buyer_acceptance_attempt,
)


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
    timeout_at = _parse_instant(event.get("timeoutAt"), "timeoutAt")
    if event.get("operation") == "ACCEPT":
        # La ASL da 30 s a waitForTaskToken; no conservar un token local
        # más allá de esa espera si el plazo general era mayor.
        timeout_at = min(timeout_at, store.now() + timedelta(seconds=29))
    return store.register_task_token(
        saga_execution_id=event.get("sagaExecutionId"),
        task_token=str(event.get("taskToken") or ""),
        wait_type=wait_type,
        timeout_at=timeout_at,
        correlation_idpk=event.get("correlationIdpk") or event.get("idpk"),
        swap_id=event.get("swapId"),
    )


def _wallet_result(result: WalletResult) -> dict[str, Any]:
    response = {
        "statusCode": result.status_code,
        "acceptedForProcessing": result.accepted_for_processing,
        "idempotencyKey": result.idempotency_key,
        "wallet": result.data,
    }
    # La ASL compara $.<reconciliation>.Payload.status. HTTP 202 no es
    # una evidencia económica y NUNCA se transforma en un estado terminal.
    if result.status_code == 200:
        status = str(result.data.get("status") or "").upper()
        if status in {"PROPOSED", "ACCEPTED", "SETTLED", "REJECTED", "EXPIRED", "CANCELLED"}:
            response["status"] = status
            for field in ("swapId", "expiresAt"):
                if result.data.get(field) is not None:
                    response[field] = result.data[field]
    return response


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
    key = event.get("idempotencyKey")
    if not isinstance(key, str) or not key:
        raise ValueError("idempotencyKey estable es obligatoria")
    if task == "buyer-acceptance":
        # Un payload falso NO puede sustituir el token válido de una espera:
        # primero verificar idpk, swapId, rol, bid y fila económica en DB.
        verify_buyer_acceptance_identity(
            store, saga_execution_id=event.get("sagaExecutionId"),
            swap_record_id=event.get("swapRecordId"),
            swap_id=event.get("swapId"), idempotency_key=key,
        )

    # La transacción de registro termina ANTES de cualquier HTTP Wallet.
    token = _register_callback(event, store)
    if token["status"] != "WAITING":
        return {"callbackAlreadyCompleted": True}

    deadline = _remaining_seconds(context)
    if task == "seller-proposal":
        swap = event.get("swap")
        if not isinstance(swap, dict):
            raise ValueError("swap es obligatorio")
        # E3-040: congelar el cuerpo del POST sin regenerar expiresAt.
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
        data = {
            "saga_execution_id": event.get("sagaExecutionId"),
            "swap_record_id": event.get("swapRecordId"),
            "swap_id": event.get("swapId"),
            "idempotency_key": key,
        }
        intent = prepare_buyer_acceptance(store, **data)
        if intent["action"] == "AWAIT_EVENT":
            return {"awaitingWalletEvent": True, "swapId": data["swap_id"],
                    "requestSkipped": intent["reason"]}
        try:
            result = wallet.accept(
                swap_id=data["swap_id"],
                idempotency_key=key,
                deadline_seconds=deadline,
            )
            returned_swap = result.data.get("swapId")
            if returned_swap is not None and returned_swap != data["swap_id"]:
                raise BuyerAcceptanceInconsistent("BUYER_ACCEPTANCE_WALLET_SWAP_ID_MISMATCH")
        except Exception as exc:
            # Reintentos de Step Functions usan la misma llave. Se registra
            # la clase del error, nunca payloads, JWT ni secretos de Wallet.
            complete_buyer_acceptance_attempt(
                store, **data, failure=type(exc).__name__,
            )
            raise
        complete_buyer_acceptance_attempt(
            store, **data, status_code=result.status_code,
            wallet_status=result.data.get("status"),
        )
        # HTTP 202 y HTTP 200 no reemplazan al hecho económico: el callback
        # lo completa el consumidor/puente sólo tras persistir evento Wallet.
        return {"awaitingWalletEvent": True,
                "statusCode": result.status_code,
                "acceptedForProcessing": result.accepted_for_processing,
                "swapId": data["swap_id"]}
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
        "compensate-seller",
        "finalize-saga",
        "validate-buyer-token",
        "check-buyer-deadline",
        "buyer-acceptance",
        "reconcile-seller-swap",
        "reconcile-buyer-swap",
        "cancel-seller-swap",
    }
    if task not in supported:
        raise SagaTaskNotImplemented(
            f"La tarea {task} permanece desactivada hasta su tarjeta funcional"
        )

    if task == "check-buyer-deadline":
        # No necesita credenciales ni DB; sólo valida instantes y margen.
        from buyer_validation import check_buyer_deadline
        return check_buyer_deadline(
            closes_at=event.get("closesAt"),
            expires_at=event.get("expiresAt"),
            minimum_remaining_seconds=event.get("minimumRemainingSeconds"),
            saga_execution_id=event.get("sagaExecutionId"),
            now=datetime.now(timezone.utc),
        )

    store = _build_store()
    if task == "validate-buyer-token":
        from buyer_validation import validate_buyer_token, jwks_settings
        jwks_url, issuer = jwks_settings(_secret(_required_env("WALLET_SECRET_ARN")))
        return validate_buyer_token(
            store,
            saga_execution_id=event.get("sagaExecutionId"),
            swap_record_id=event.get("swapRecordId"),
            swap_id=event.get("swapId"),
            expected=event.get("expected"),
            jwks_url=jwks_url,
            issuer=issuer,
        )
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
    if task == "compensate-seller":
        return compensate_seller(
            store, bid_idpk=event.get("bidIdpk"), offer_id=event.get("offerId"),
            saga_execution_id=event.get("sagaExecutionId"),
        )
    if task == "finalize-saga":
        if event.get("role") != "SELLER":
            raise SagaTaskNotImplemented("finalize-saga BUYER pendiente de E3-046")
        return finalize_seller(
            store, saga_execution_id=event.get("sagaExecutionId"),
            status=event.get("status"),
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
        if task == "reconcile-seller-swap":
            saga_idpk = event.get("sagaExecutionId")
            key = event.get("idempotencyKey") or saga_idpk
            known = known_seller_swap_id(
                store, saga_execution_id=saga_idpk, idempotency_key=key,
            )
            if swap_id is not None and swap_id != known:
                raise SellerTerminalInconsistent("SELLER_RECONCILIATION_SWAP_ID_MISMATCH")
            swap_id = known
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
