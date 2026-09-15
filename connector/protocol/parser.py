from datetime import datetime
from uuid import UUID
from .models import ProtocolEnvelope


class EnvelopeParseError(ValueError):
    #Error base para envelopes v2 que pueden leerse, pero no parsearse correctamente.
    pass


class MissingMsgIdError(EnvelopeParseError):
    #Caso especial, si no existe msgId no hay un mensaje válido al cual responder.
    #Esto permitirá tratarlo de forma distinta en la lógica posterior.
    pass


def parse_envelope(payload: dict) -> ProtocolEnvelope:
    #msgId se trata por separado porque el protocolo indica que un mensaje sin este campo debe descartarse y registrarse.
    if "msgId" not in payload:
        raise MissingMsgIdError("El mensaje no contiene msgId")

    #Campos mínimos exigidos por todo el mensaje del protocolo v2
    required_fields = ("idpk", "msgId", "type", "timestamp")

    #Se calculan los campos faltanres
    missing = [field for field in required_fields if field not in payload]

    #Se analiza si hay campos faltantes, si es así se lanza error
    if missing:
        raise EnvelopeParseError(f"Faltan campos obligatorios: {', '.join(missing)}")

    try:
        #Se normalizan los identificadores
        idpk = UUID(str(payload["idpk"]))
        msg_id = UUID(str(payload["msgId"]))
    except (ValueError, TypeError, AttributeError) as exc:
        raise EnvelopeParseError("idpk y msgId deben ser UUID válidos") from exc

    #En esta capa solo se verifica que exista un tipo no vacío.
    if not isinstance(payload["type"], str) or not payload["type"]:
        raise EnvelopeParseError("type debe ser un string no vacío")

    #El timestamp del protocolo viene en ISO 8601. Se transforma a datetime para evitar trabajar con fechas como strings
    try:
        timestamp = datetime.fromisoformat(str(payload["timestamp"]).replace("Z", "+00:00"))
    except ValueError as exc:
        raise EnvelopeParseError("timestamp debe ser ISO 8601") from exc

    #El contenido específico del mensaje viaja en data en protocolo v2.
    data = payload.get("data")

    if data is not None and not isinstance(data, dict):
        raise EnvelopeParseError("data debe ser un objeto JSON")

    #Los campos opcionales se mantienen como None cuando no vienen presentes.
    #raw conserva el mensaje original por si acaso es necesario.
    return ProtocolEnvelope(
        idpk=idpk,
        msg_id=msg_id,
        type=payload["type"],
        timestamp=timestamp,
        data=data,
        city_id=payload.get("cityId"),
        sender=payload.get("sender"),
        cycle_id=payload.get("cycleId"),
        raw=payload,
    )