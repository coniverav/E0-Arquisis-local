from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any, Callable, Mapping


TERMINAL_SAGA_STATUSES = {"SETTLED", "REJECTED", "COMPENSATED", "TIMED_OUT", "FAILED"}


def database_dsn(secret: Mapping[str, Any]) -> str:
    direct = secret.get("DATABASE_URL") or secret.get("url")
    if isinstance(direct, str) and direct:
        return direct.replace("postgresql+psycopg://", "postgresql://", 1)
    required = ("host", "username", "password", "dbname")
    if not all(isinstance(secret.get(key), str) and secret[key] for key in required):
        raise ValueError("El secret de base de datos está incompleto")
    port = int(secret.get("port", 5432))
    from urllib.parse import quote

    return (
        f"postgresql://{quote(secret['username'])}:{quote(secret['password'])}"
        f"@{secret['host']}:{port}/{quote(secret['dbname'])}"
    )


class PostgresStore:
    """Adaptador transaccional para tablas ya creadas por E3-016/E2-071."""

    def __init__(
        self,
        dsn: str,
        *,
        city_id: str,
        connect: Callable[..., Any] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if connect is None:
            import psycopg

            connect = psycopg.connect
        self.dsn = dsn
        self.city_id = city_id
        self.connect = connect
        self.now = now or (lambda: datetime.now(timezone.utc))

    def register_task_token(
        self,
        *,
        saga_execution_id: int | str,
        task_token: str,
        wait_type: str,
        timeout_at: datetime,
        correlation_idpk: str | None,
        swap_id: str | None,
    ) -> dict[str, Any]:
        if saga_execution_id is None:
            raise ValueError("sagaExecutionId es obligatorio")
        if not task_token or (correlation_idpk is None and swap_id is None):
            raise ValueError("Task token y correlación son obligatorios")
        now = self.now()
        if timeout_at <= now:
            raise ValueError("El task token ya está vencido")
        token_hash = sha256(task_token.encode("utf-8")).hexdigest()

        with self.connect(self.dsn) as connection:
            with connection.cursor() as cursor:
                execution = self._lock_execution(cursor, saga_execution_id)
                if execution is None:
                    raise ValueError("La ejecución local de saga no existe")
                execution_id, status = execution
                if status in TERMINAL_SAGA_STATUSES:
                    raise ValueError("La saga ya está en estado terminal")

                cursor.execute(
                    """
                    SELECT id, saga_execution_id, wait_type, correlation_idpk,
                           swap_id, timeout_at, status
                    FROM saga_task_tokens WHERE task_token_hash = %s
                    FOR UPDATE
                    """,
                    (token_hash,),
                )
                existing = cursor.fetchone()
                if existing is not None:
                    same = (
                        existing[1] == execution_id
                        and existing[2] == wait_type
                        and existing[3] == correlation_idpk
                        and existing[4] == swap_id
                        and existing[5] == timeout_at
                    )
                    if not same:
                        raise ValueError("Task token reutilizado con otra correlación")
                    return {"id": existing[0], "status": existing[6], "created": False}

                cursor.execute(
                    """
                    UPDATE saga_task_tokens
                    SET status = 'SUPERSEDED', completed_at = %s,
                        claim_token = NULL, claim_until = NULL
                    WHERE saga_execution_id = %s AND wait_type = %s
                      AND status IN ('WAITING', 'DISPATCHING')
                    """,
                    (now, execution_id, wait_type),
                )
                cursor.execute(
                    """
                    INSERT INTO saga_task_tokens (
                        saga_execution_id, task_token, task_token_hash, wait_type,
                        correlation_idpk, swap_id, status, timeout_at, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, 'WAITING', %s, %s)
                    RETURNING id
                    """,
                    (
                        execution_id, task_token, token_hash, wait_type,
                        correlation_idpk, swap_id, timeout_at, now,
                    ),
                )
                token_id = cursor.fetchone()[0]
                cursor.execute(
                    """
                    UPDATE saga_executions SET status = 'WAITING', updated_at = %s
                    WHERE id = %s
                    """,
                    (now, execution_id),
                )
            connection.commit()
        return {"id": token_id, "status": "WAITING", "created": True}

    @staticmethod
    def _lock_execution(cursor: Any, identity: int | str) -> tuple[int, str] | None:
        if isinstance(identity, int) or (isinstance(identity, str) and identity.isdigit()):
            cursor.execute(
                "SELECT id, status FROM saga_executions WHERE id = %s FOR UPDATE",
                (int(identity),),
            )
        elif isinstance(identity, str) and identity.startswith("arn:"):
            cursor.execute(
                "SELECT id, status FROM saga_executions WHERE execution_arn = %s FOR UPDATE",
                (identity,),
            )
        else:
            cursor.execute(
                "SELECT id, status FROM saga_executions WHERE idpk = %s FOR UPDATE",
                (str(identity),),
            )
        return cursor.fetchone()

    def reserve(self, category: str) -> int:
        limits = {"wallet:read": 300, "wallet:write": 120}
        if category not in limits:
            raise ValueError("Categoría de cuota no soportada")
        now = self.now()
        key = f"{self.city_id}:{category}"
        with self.connect(self.dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO operational_rate_limits (key, attempts, blocked_until)
                    VALUES (%s, '[]'::jsonb, NULL) ON CONFLICT (key) DO NOTHING
                    """,
                    (key,),
                )
                cursor.execute(
                    """
                    SELECT attempts, blocked_until FROM operational_rate_limits
                    WHERE key = %s FOR UPDATE
                    """,
                    (key,),
                )
                attempts, blocked_until = cursor.fetchone()
                if blocked_until is not None and blocked_until > now:
                    return max(1, int((blocked_until - now).total_seconds() + 0.999))
                recent = sorted(
                    datetime.fromisoformat(value).astimezone(timezone.utc)
                    for value in (attempts or [])
                    if datetime.fromisoformat(value).astimezone(timezone.utc)
                    > now - timedelta(seconds=60)
                )
                if len(recent) >= limits[category]:
                    return max(
                        1,
                        int((recent[0] + timedelta(seconds=60) - now).total_seconds() + 0.999),
                    )
                recent.append(now)
                cursor.execute(
                    "UPDATE operational_rate_limits SET attempts = %s::jsonb WHERE key = %s",
                    (json.dumps([value.isoformat() for value in recent]), key),
                )
            connection.commit()
        return 0

    def block(self, category: str, seconds: int) -> None:
        if seconds < 0:
            raise ValueError("Retry-After inválido")
        deadline = self.now() + timedelta(seconds=seconds)
        key = f"{self.city_id}:{category}"
        with self.connect(self.dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO operational_rate_limits (key, attempts, blocked_until)
                    VALUES (%s, '[]'::jsonb, %s)
                    ON CONFLICT (key) DO UPDATE SET blocked_until = GREATEST(
                        operational_rate_limits.blocked_until, EXCLUDED.blocked_until
                    )
                    """,
                    (key, deadline),
                )
            connection.commit()

    def task_token_status(self, task_token: str) -> str | None:
        token_hash = sha256(task_token.encode("utf-8")).hexdigest()
        with self.connect(self.dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT status FROM saga_task_tokens WHERE task_token_hash = %s",
                    (token_hash,),
                )
                row = cursor.fetchone()
        return row[0] if row else None

    def mark_callback_succeeded(self, task_token: str) -> bool:
        token_hash = sha256(task_token.encode("utf-8")).hexdigest()
        with self.connect(self.dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE saga_task_tokens
                    SET status = 'SUCCEEDED', completed_at = %s
                    WHERE task_token_hash = %s AND status = 'WAITING'
                    """,
                    (self.now(), token_hash),
                )
                changed = cursor.rowcount == 1
            connection.commit()
        return changed
