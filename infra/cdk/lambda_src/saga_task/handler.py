import json
import logging
import os
from typing import Any


logger = logging.getLogger()
logger.setLevel(logging.INFO)


class SagaTaskNotImplemented(RuntimeError):
    """Evita ejecutar parcialmente una tarea pendiente de implementación."""


def _correlation(event: dict[str, Any]) -> dict[str, Any]:
    return {
        key: event[key]
        for key in ("cycleId", "swapId", "idpk", "correlationIdpk", "sagaExecutionId")
        if event.get(key) is not None
    }


def lambda_handler(event: dict[str, Any], context: Any) -> None:
    task = os.environ.get("SAGA_TASK")
    if not task:
        raise RuntimeError("SAGA_TASK no está configurado")

    logger.info(
        json.dumps(
            {
                "event": "saga_task_invoked",
                "task": task,
                "requestId": getattr(context, "aws_request_id", None),
                **_correlation(event),
            },
            sort_keys=True,
        )
    )
    raise SagaTaskNotImplemented(
        f"La tarea {task} está declarada pero su adaptador se implementa "
        "en E3-022/E3-023"
    )
