import importlib
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4
from decimal import Decimal

from sqlmodel import Session, select

from app.config import CITY_ID, DATABASE_URL
from app.database import engine
from app.models import Cycle, MarketOffer, MarketBid, SagaTaskToken
from app.services.seller_bid_acceptance import reserve_incoming_bid


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



def test_callback_token_is_committed_before_wallet_fast_event(monkeypatch):
    monkeypatch.setenv("E3_SELLER_SAGA_ENABLED", "true")
    monkeypatch.setenv(
        "E3_SELLER_STATE_MACHINE_ARN",
        "arn:aws:states:us-east-1:123456789012:stateMachine:test-e3023-seller",
    )

    now = datetime.now(timezone.utc)
    idpk = str(uuid4())
    cycle_id = f"cycle-e3-023-{uuid4()}"
    offer_id = f"{CITY_ID}-{cycle_id}"
    timeout_at = now + timedelta(minutes=5)

    # Crear los registros económicos requeridos por E3-040.
    with Session(engine) as session:
        session.add(Cycle(
            cycle_id=cycle_id,
            status_idpk=str(uuid4()),
            created_at=now,
            market_opens_at=now - timedelta(minutes=1),
            market_last_offer_at=now + timedelta(minutes=4),
            market_last_bid_at=now + timedelta(minutes=3),
            market_closes_at=now + timedelta(minutes=5),
            valid_until=now + timedelta(minutes=6),
            status_payload={"market": {"minBid": 5}},
        ))

        session.add(MarketOffer(
            offer_id=offer_id,
            cycle_id=cycle_id,
            city_id=CITY_ID,
            is_local=True,
            initial_quantity=100,
            remaining_quantity=100,
            price_per_energy=Decimal("4.25"),
            status="ACTIVE",
            published_at=now,
            last_updated_at=now,
        ))

        session.add(MarketBid(
            idpk=idpk,
            msg_id=str(uuid4()),
            cycle_id=cycle_id,
            direction="INCOMING",
            offer_id=offer_id,
            counterpart_city_id="COR",
            quantity=40,
            status="PENDING",
            created_at=now,
        ))

        session.commit()

    # La reserva crea de forma transaccional:
    # - HOLD
    # - SwapRecord
    # - SagaExecution
    # - Intencion outbox
    with Session(engine) as session:
        result = reserve_incoming_bid(
            session,
            bid_idpk=idpk,
            now=now,
        )
        assert result["status"] == "RESERVED"
        session.commit()

    store = store_module.PostgresStore(
        DATABASE_URL.replace(
            "postgresql+psycopg://",
            "postgresql://",
            1,
        ),
        city_id=CITY_ID,
    )

    class FastWallet:
        def propose(self, **kwargs):
            # Simula que el evento Wallet llega antes
            # de la respuesta HTTP del POST.
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

    # La identidad de la saga es el idpk,
    # no el ID entero de PostgreSQL.
    event = {
        "taskToken": "task-token-fast-event",
        "sagaExecutionId": idpk,
        "correlationIdpk": idpk,
        "idempotencyKey": idpk,
        "operation": "PROPOSE",
        "swap": {
            "idpk": idpk,
            "idempotencyKey": idpk,
            "cancelIdempotencyKey": str(uuid4()),
            "give": {
                "asset": "KWH",
                "amount": "40",
            },
            "take": {
                "asset": "CR",
                "amount": "170.00",
            },
            "memo": offer_id,
        },
        "timeoutAt": timeout_at.isoformat(),
    }

    context = type(
        "Context",
        (),
        {"get_remaining_time_in_millis": lambda self: 20000},
    )()

    result = handler._callback_wallet_task(
        "seller-proposal",
        event,
        context,
        store,
        FastWallet(),
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
