import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

# La URL de la base de datos se arma con variables de entorno.
# En docker-compose se las pasamos automáticamente, no hay que escribirlas a mano.
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://energyshark:energyshark@db:5432/energyshark",
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
