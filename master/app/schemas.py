from datetime import datetime
from typing import Optional, Any
from pydantic import BaseModel, Field, ConfigDict


class EventIn(BaseModel):
    """Lo que el connector nos envía por HTTP POST cada vez que recibe un mensaje."""
    idpk: str
    type: str
    packageBody: dict[str, Any]
    receivedAt: Optional[datetime] = None  # si no llega, master pone la hora actual


class EventOut(BaseModel):
    # El nombre del campo es igual al de la columna en la base de datos
    # (snake_case), así la lectura desde el objeto de SQLAlchemy funciona
    # directo, sin buscar ningún alias. serialization_alias es un nombre
    # aparte que SOLO se usa al armar el JSON de salida (camelCase).
    id: int
    idpk: str
    type: str
    package_body: dict[str, Any] = Field(serialization_alias="packageBody")
    #valid_until: Optional[datetime] = Field(default=None, serialization_alias="validUntil")
    received_at: datetime = Field(serialization_alias="receivedAt")

    model_config = ConfigDict(from_attributes=True)
