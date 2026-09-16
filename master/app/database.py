# Hace la conexión con la base de datos PostgreSQL y crea las tablas si no existen
# También define un generador de sesiones SQLAlchemy/SQLModel

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlmodel import SQLModel, Session, create_engine

from .config import DATABASE_URL

# pool_pre_ping evita reutilizar conexiones TCP muertas después de pausas/reinicios.
engine = create_engine(DATABASE_URL, pool_pre_ping=True)

# Se actualiza por esta, que aplica todas las migraciones versionadas pendientes de PostgreSQL.
def run_migrations() -> None:
    """Aplica todas las migraciones versionadas pendientes de PostgreSQL."""

    alembic_ini = Path(__file__).resolve().parents[1] / "alembic.ini"
    config = Config(str(alembic_ini))

    command.upgrade(config, "head")


def get_session():
    with Session(engine) as session:
        yield session
