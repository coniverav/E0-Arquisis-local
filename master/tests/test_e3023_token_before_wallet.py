import importlib
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from sqlmodel import Session, select

from app.config import CITY_ID, DATABASE_URL
from app.database import engine
from app.models import SagaTaskToken
from app.services.saga_tokens import get_or_create_saga_execution


LAMBDA_SOURCE = (
    Path(__file__).resolve().parents[2]
    / "infra"
    / "cdk"
    / "lambda_src"
    / "saga_task"
)
sys.path.insert(0, str(LAMBDA_SOURCE))
handler = importlib.import_module("handler")
store_module = importlib.import_module("postgres_store")
wallet_module = importlib.import_module("wallet_client")


def test_callback_token_is_committed_before_wallet_fast_event():
    now = datetime.now(timezone.utc)
    idpk = str(uuid4())
    timeout_at = now + timedelta(minutes=5)
    with Session(engine) as session:
        execution, _ = get_or_create_saga_execution(
            session,
            idpk=idpk,
            role="SELLER",
            cycle_id="cycle-e3-023",
            timeout_at=timeout_at,
            now=now,
        )
        session.commit()
        execution_id = execution.id

    store = store_module.PostgresStore(
        DATABASE_URL.replace("postgresql+psycopg://", "postgresql://", 1),
        city_id=CITY_ID,
    )

    class FastWallet:
        def propose(self, **kwargs):
            # Otra sesión representa al consumidor que recibe el evento antes
            # de que el POST haya devuelto su respuesta a Lambda.
            with Session(engine) as event_session:
                token = event_session.exec(
                    select(SagaTaskToken).where(
                        SagaTaskToken.correlation_idpk == idpk,
                        SagaTaskToken.status == "WAITING",
                    )
                ).one()
                token.status = "SUCCEEDED"
                token.callback_idpk = str(uuid4())
                token.completed_at = datetime.now(timezone.utc)
                event_session.add(token)
                event_session.commit()
            return wallet_module.WalletResult(
                202,
                {"swapId": "swap-fast", "status": "accepted"},
                True,
                idpk,
            )

    event = {
        "taskToken": "task-token-fast-event",
        "sagaExecutionId": execution_id,
        "correlationIdpk": idpk,
        "idempotencyKey": idpk,
        "operation": "PROPOSE",
        "swap": {"give": {"asset": "KWH", "amount": "10"}},
        "timeoutAt": timeout_at.isoformat(),
    }
    context = type(
        "Context",
        (),
        {"get_remaining_time_in_millis": lambda self: 20000},
    )()
    result = handler._callback_wallet_task(
        "seller-proposal", event, context, store, FastWallet()
    )

    assert result["statusCode"] == 202
    assert result["acceptedForProcessing"] is True
    with Session(engine) as session:
        tokens = session.exec(select(SagaTaskToken)).all()
        assert len(tokens) == 1
        assert tokens[0].status == "SUCCEEDED"
        assert tokens[0].correlation_idpk == idpk


def test_database_secret_dsn_never_requires_committed_credentials():
    assert store_module.database_dsn({"DATABASE_URL": DATABASE_URL}).startswith(
        "postgresql://"
    )


def test_lambda_wallet_quota_is_shared_in_postgresql():
    now = datetime.now(timezone.utc)
    dsn = DATABASE_URL.replace("postgresql+psycopg://", "postgresql://", 1)
    first = store_module.PostgresStore(dsn, city_id=CITY_ID, now=lambda: now)
    second = store_module.PostgresStore(dsn, city_id=CITY_ID, now=lambda: now)

    for _ in range(120):
        assert first.reserve("wallet:write") == 0
    assert second.reserve("wallet:write") == 60

    first.block("wallet:read", 7)
    assert second.reserve("wallet:read") == 7
