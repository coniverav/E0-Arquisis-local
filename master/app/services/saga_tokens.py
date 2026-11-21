"""Persistencia y resolución de callbacks de Step Functions Standard."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256

from sqlmodel import Session, select

from app.models import SagaExecution, SagaTaskToken


SAGA_ROLES = frozenset({"SELLER", "BUYER"})
TERMINAL_SAGA_STATUSES = frozenset(
    {"SETTLED", "REJECTED", "COMPENSATED", "TIMED_OUT", "FAILED"}
)
TOKEN_COMPLETION_STATUSES = frozenset({"SUCCEEDED", "FAILED"})


class SagaCorrelationError(ValueError):
    """La misma identidad económica apunta a datos incompatibles."""


class AmbiguousSagaTokenError(RuntimeError):
    """Más de un token activo coincide con la misma clave de callback."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _token_hash(task_token: str) -> str:
    return sha256(task_token.encode("utf-8")).hexdigest()


def get_or_create_saga_execution(
    session: Session,
    *,
    idpk: str,
    role: str,
    timeout_at: datetime,
    cycle_id: str | None = None,
    swap_id: str | None = None,
    now: datetime | None = None,
) -> tuple[SagaExecution, bool]:
    """Crea una sola ejecución local por idpk o valida el replay existente."""

    now = now or _utcnow()
    normalized_role = role.upper()
    if normalized_role not in SAGA_ROLES:
        raise ValueError(f"Rol de saga no soportado: {role}")
    if timeout_at <= now:
        raise ValueError("El timeout de la saga debe estar en el futuro")

    existing = session.exec(
        select(SagaExecution).where(SagaExecution.idpk == idpk)
    ).one_or_none()
    if existing is not None:
        expected = {
            "role": normalized_role,
            "cycle_id": cycle_id,
            "swap_id": swap_id,
            "timeout_at": timeout_at,
        }
        for field, value in expected.items():
            current = getattr(existing, field)
            if value is not None and current is not None and current != value:
                raise SagaCorrelationError(
                    f"Replay de saga incompatible para {field}"
                )
        if swap_id is not None and existing.swap_id is None:
            attach_swap_id(session, existing, swap_id=swap_id, now=now)
        return existing, False

    execution = SagaExecution(
        idpk=idpk,
        swap_id=swap_id,
        cycle_id=cycle_id,
        role=normalized_role,
        status="PENDING_START",
        timeout_at=timeout_at,
        created_at=now,
        updated_at=now,
    )
    session.add(execution)
    session.flush()
    return execution, True


def attach_execution_arn(
    session: Session,
    execution: SagaExecution,
    *,
    execution_arn: str,
    now: datetime | None = None,
) -> None:
    """Vincula una única ejecución AWS sin permitir reemplazarla en un replay."""

    now = now or _utcnow()
    if execution.execution_arn == execution_arn:
        return
    if execution.execution_arn is not None:
        raise SagaCorrelationError("La saga ya tiene otra ejecución AWS")
    owner = session.exec(
        select(SagaExecution).where(
            SagaExecution.execution_arn == execution_arn
        )
    ).one_or_none()
    if owner is not None and owner.id != execution.id:
        raise SagaCorrelationError("La ejecución AWS ya pertenece a otra saga")
    execution.execution_arn = execution_arn
    execution.status = "RUNNING"
    execution.updated_at = now
    session.add(execution)


def attach_swap_id(
    session: Session,
    execution: SagaExecution,
    *,
    swap_id: str,
    now: datetime | None = None,
) -> None:
    """Asocia el swapId autoritativo una sola vez."""

    now = now or _utcnow()
    if execution.swap_id == swap_id:
        return
    if execution.swap_id is not None:
        raise SagaCorrelationError("La saga ya está asociada a otro swapId")
    owner = session.exec(
        select(SagaExecution).where(SagaExecution.swap_id == swap_id)
    ).one_or_none()
    if owner is not None and owner.id != execution.id:
        raise SagaCorrelationError("El swapId ya pertenece a otra saga")
    execution.swap_id = swap_id
    execution.updated_at = now
    session.add(execution)


def register_task_token(
    session: Session,
    execution: SagaExecution,
    *,
    task_token: str,
    wait_type: str,
    timeout_at: datetime,
    correlation_idpk: str | None = None,
    swap_id: str | None = None,
    now: datetime | None = None,
) -> tuple[SagaTaskToken, bool]:
    """Guarda el token antes de efectuar la operación HTTP asociada."""

    now = now or _utcnow()
    if execution.id is None:
        raise ValueError("La ejecución debe estar persistida")
    if not task_token:
        raise ValueError("El task token es obligatorio")
    if correlation_idpk is None and swap_id is None:
        raise ValueError("Se requiere idpk o swapId para correlacionar el callback")
    if timeout_at <= now:
        raise ValueError("El task token debe tener un timeout futuro")

    token_hash = _token_hash(task_token)
    existing = session.exec(
        select(SagaTaskToken).where(
            SagaTaskToken.task_token_hash == token_hash
        )
    ).one_or_none()
    if existing is not None:
        same_identity = (
            existing.saga_execution_id == execution.id
            and existing.wait_type == wait_type
            and existing.correlation_idpk == correlation_idpk
            and existing.swap_id == swap_id
            and existing.timeout_at == timeout_at
        )
        if not same_identity:
            raise SagaCorrelationError("Task token reutilizado con otra correlación")
        return existing, False

    previous_tokens = session.exec(
        select(SagaTaskToken).where(
            SagaTaskToken.saga_execution_id == execution.id,
            SagaTaskToken.wait_type == wait_type,
            SagaTaskToken.status == "WAITING",
        )
    ).all()
    for previous in previous_tokens:
        previous.status = "SUPERSEDED"
        previous.completed_at = now
        session.add(previous)

    token = SagaTaskToken(
        saga_execution_id=execution.id,
        task_token=task_token,
        task_token_hash=token_hash,
        wait_type=wait_type,
        correlation_idpk=correlation_idpk,
        swap_id=swap_id,
        status="WAITING",
        timeout_at=timeout_at,
        created_at=now,
    )
    execution.status = "WAITING"
    execution.updated_at = now
    session.add(token)
    session.add(execution)
    session.flush()
    return token, True


def find_waiting_task_token(
    session: Session,
    *,
    correlation_idpk: str | None = None,
    swap_id: str | None = None,
    role: str | None = None,
    execution_arn: str | None = None,
    wait_type: str | None = None,
    now: datetime | None = None,
) -> SagaTaskToken | None:
    """Busca primero por idpk y sólo entonces usa swapId como fallback."""

    if correlation_idpk is None and swap_id is None:
        raise ValueError("Se requiere idpk o swapId")
    now = now or _utcnow()

    def matches(column, value: str) -> list[SagaTaskToken]:
        statement = (
            select(SagaTaskToken)
            .join(
                SagaExecution,
                SagaExecution.id == SagaTaskToken.saga_execution_id,
            )
            .where(
                SagaTaskToken.status == "WAITING",
                SagaTaskToken.timeout_at > now,
                column == value,
            )
        )
        if role is not None:
            statement = statement.where(SagaExecution.role == role.upper())
        if execution_arn is not None:
            statement = statement.where(
                SagaExecution.execution_arn == execution_arn
            )
        if wait_type is not None:
            statement = statement.where(SagaTaskToken.wait_type == wait_type)
        return list(session.exec(statement).all())

    candidates: list[SagaTaskToken] = []
    if correlation_idpk is not None:
        candidates = matches(
            SagaTaskToken.correlation_idpk,
            correlation_idpk,
        )
    if not candidates and swap_id is not None:
        candidates = matches(SagaTaskToken.swap_id, swap_id)
    if len(candidates) > 1:
        raise AmbiguousSagaTokenError("Más de un task token activo coincide")
    return candidates[0] if candidates else None


def complete_task_token(
    session: Session,
    token: SagaTaskToken,
    *,
    status: str,
    callback_idpk: str | None,
    now: datetime | None = None,
) -> bool:
    """Marca el callback una vez; replays posteriores no cambian su resultado."""

    normalized_status = status.upper()
    if normalized_status not in TOKEN_COMPLETION_STATUSES:
        raise ValueError(f"Estado de callback no soportado: {status}")
    if token.status != "WAITING":
        return False
    token.status = normalized_status
    token.callback_idpk = callback_idpk
    token.completed_at = now or _utcnow()
    session.add(token)
    return True


def expire_waiting_task_tokens(
    session: Session,
    *,
    now: datetime | None = None,
) -> list[int]:
    """Expira esperas vencidas sin inferir un resultado económico."""

    now = now or _utcnow()
    expired = session.exec(
        select(SagaTaskToken).where(
            SagaTaskToken.status == "WAITING",
            SagaTaskToken.timeout_at <= now,
        )
    ).all()
    identifiers: list[int] = []
    for token in expired:
        token.status = "EXPIRED"
        token.completed_at = now
        session.add(token)
        if token.id is not None:
            identifiers.append(token.id)
    return identifiers


def mark_saga_terminal(
    session: Session,
    execution: SagaExecution,
    *,
    status: str,
    now: datetime | None = None,
) -> bool:
    """Registra un estado terminal explícito sin reabrir una saga cerrada."""

    normalized_status = status.upper()
    if normalized_status not in TERMINAL_SAGA_STATUSES:
        raise ValueError(f"Estado terminal no soportado: {status}")
    if execution.status in TERMINAL_SAGA_STATUSES:
        return False
    now = now or _utcnow()
    execution.status = normalized_status
    execution.terminal_at = now
    execution.updated_at = now
    session.add(execution)
    return True
