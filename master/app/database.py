# Hace la conexión con la base de datos PostgreSQL y crea las tablas si no existen
# También define un generador de sesiones SQLAlchemy/SQLModel

from sqlmodel import SQLModel, Session, create_engine

from .config import DATABASE_URL

# pool_pre_ping evita reutilizar conexiones TCP muertas después de pausas/reinicios.
engine = create_engine(DATABASE_URL, pool_pre_ping=True)


def create_db_and_tables() -> None:
    """Crea las tablas de forma segura cuando master1/master2 arrancan a la vez."""
    with engine.begin() as connection:
        # Lock transaccional de PostgreSQL: solo una réplica ejecuta create_all a la vez.
        connection.exec_driver_sql("SELECT pg_advisory_xact_lock(2173)") # Lock arbitrario, pero fijo, para que master1/master2 no se pisen
        SQLModel.metadata.create_all(connection)


def get_session():
    with Session(engine) as session:
        yield session
