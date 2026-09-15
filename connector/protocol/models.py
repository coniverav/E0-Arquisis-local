from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class ProtocolEnvelope:
    #Campos obligatorios presentes en todo mensaje del protocolo.
    idpk: UUID
    msg_id: UUID
    type: str
    timestamp: datetime

    #Contenido específico del tipo de mensaje.
    data: dict[str, Any] | None

    #Identidad del emisor. Según el caso se utiliza city_id o sender.
    city_id: str | None = None
    sender: str | None = None

    #Algunos mensajes están asociados a un ciclo y otros no.
    cycle_id: str | None = None

    #Mensaje original para conservar campos no modelados explícitamente
    #como reason, code u otros datos propios de ciertos tipos.
    raw: dict[str, Any] | None = None

@dataclass(frozen=True)
class ValidationResult:
    #Resultado de validar un mensaje contra las reglas del protocolo.

    #Es válido o no
    valid: bool

    #La razón, código y mensaje
    reason: str | None = None
    code: int | None = None
    message: str | None = None