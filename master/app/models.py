from sqlalchemy import Column, Integer, String, DateTime
from sqlalchemy.dialects.postgresql import JSONB
from .database import Base


class DemandEvent(Base):
    """
    Cada fila = un evento 'demand-set' recibido desde el connector.
    'id' es el identificador que nosotros generamos (autoincremental)
    y que se usa en la URL /history/{id} (RF2/RF4).
    'idpk' es el identificador que viene en el mensaje original (UUIDv4),
    lo guardamos aparte para no repetir eventos duplicados.
    """
    __tablename__ = "demand_events"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    idpk = Column(String, unique=True, index=True, nullable=False)
    type = Column(String, index=True, nullable=False)
    package_body = Column(JSONB, nullable=False)  # todo el packageBody tal cual llegó
    valid_until = Column(DateTime(timezone=True), nullable=True, index=True)
    received_at = Column(DateTime(timezone=True), nullable=False, index=True)
