"""Prueba la migración E2 -> E3 en una base PostgreSQL descartable."""

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

import app.config as app_config


def test_additive_saga_migration_preserves_e2_swap(monkeypatch):
    source_url = make_url(app_config.DATABASE_URL)
    source_database = source_url.database or ""
    assert source_database.startswith("energyshark_test_")

    database_name = f"energyshark_test_e3016_{uuid4().hex[:10]}"
    admin_url = source_url.set(database="postgres")
    isolated_url = source_url.set(database=database_name)
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    quoted_name = admin_engine.dialect.identifier_preparer.quote(database_name)

    with admin_engine.connect() as connection:
        connection.execute(text(f"CREATE DATABASE {quoted_name}"))

    isolated_engine = create_engine(isolated_url)
    try:
        monkeypatch.setattr(
            app_config,
            "DATABASE_URL",
            isolated_url.render_as_string(hide_password=False),
        )
        alembic_config = Config(
            str(Path(__file__).resolve().parents[1] / "alembic.ini")
        )
        command.upgrade(alembic_config, "e3026outboxclaims")

        old_idpk = str(uuid4())
        with isolated_engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO swap_records (
                        idpk, cycle_id, role, counterpart_city_id,
                        give_asset, give_amount, take_asset, take_amount,
                        status, idempotency_key, created_at
                    ) VALUES (
                        :idpk, :cycle_id, 'PROPOSER', 'COR',
                        'KWH', 10, 'CR', 2000,
                        'SUBMITTED', :idpk, :created_at
                    )
                    """
                ),
                {
                    "idpk": old_idpk,
                    "cycle_id": "cycle-e2-preserved",
                    "created_at": datetime.now(timezone.utc),
                },
            )

        command.upgrade(alembic_config, "head")

        inspector = inspect(isolated_engine)
        assert "saga_executions" in inspector.get_table_names()
        assert "saga_task_tokens" in inspector.get_table_names()
        saga_indexes = {
            index["name"] for index in inspector.get_indexes("saga_task_tokens")
        }
        assert "ix_saga_task_tokens_status_correlation_idpk" in saga_indexes
        assert "ix_saga_task_tokens_status_swap_id" in saga_indexes

        with isolated_engine.connect() as connection:
            preserved = connection.execute(
                text(
                    "SELECT idpk, cycle_id, status FROM swap_records "
                    "WHERE idpk = :idpk"
                ),
                {"idpk": old_idpk},
            ).one()
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
        assert preserved == (old_idpk, "cycle-e2-preserved", "SUBMITTED")
        assert revision == "e3016sagatokens"
    finally:
        isolated_engine.dispose()
        with admin_engine.connect() as connection:
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :database_name"
                ),
                {"database_name": database_name},
            )
            connection.execute(text(f"DROP DATABASE {quoted_name}"))
        admin_engine.dispose()
