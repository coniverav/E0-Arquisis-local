from datetime import datetime
from uuid import UUID
from .models import ProtocolEnvelope, ValidationResult
from .parser import EnvelopeParseError, MissingMsgIdError, parse_envelope


#Tipos definidos por el protocolo v2.
KNOWN_TYPES = {
    "ack",
    "nack",
    "error",
    "request",
    "status-statement",
    "transfer",
    "demand-statement",
    "negotiation-proposal",
    "give",
    "take",
    "negotiation-report",
    "distance-table",
}


#Algunos mensajes solo pueden venir de la central y otros de una ciudad.
CENTRAL_TYPES = {
    "status-statement",
    "demand-statement",
    "give",
    "take",
    "error",
}

CITY_TYPES = {
    "request",
    "negotiation-proposal",
    "negotiation-report",
}

NACK_CODES = {
    "MALFORMED_MESSAGE": 422,
    "UNKNOWN_TYPE": 400,
    "IDPK_EQUALS_MSGID": 422,
    "IDENTITY_MISMATCH": 403,
}

ERROR_CODES = {
    "CYCLE_UNKNOWN": 404,
    "CYCLE_EXPIRED": 410,
    "PRICE_ABOVE_CAP": 422,
    "OVER_CAPACITY": 409,
}

#Construye un resultado MALFORMED_MESSAGE consistente.
def _malformed(message: str) -> ValidationResult:
    return ValidationResult(
        valid=False,
        reason="MALFORMED_MESSAGE",
        code=422,
        message=message,
    )

#Acepta int/float, pero no bool.
def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)

#Función para ver si es positivo.
def _is_positive_number(value) -> bool:
    return _is_number(value) and value > 0

#Función para ver si es uuid
def _is_uuid(value) -> bool:
    try:
        UUID(str(value))
        return True
    except (ValueError, TypeError, AttributeError):
        return False

#Comprueba un datetime ISO 8601 con zona horaria.
def _is_datetime(value) -> bool:
    if not isinstance(value, str):
        return False

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.tzinfo is not None
    except ValueError:
        return False

#Valida la forma de identificar al emisor dentro del envelope.
def _validate_identity(message: ProtocolEnvelope) -> ValidationResult | None:

    #Un mensaje no puede declararse simultáneamente como central y ciudad.
    if message.sender is not None and message.city_id is not None:
        return _malformed("El mensaje no puede contener sender y cityId simultáneamente")

    if message.type in CENTRAL_TYPES:
        if message.sender != "central" or message.city_id is not None:
            return _malformed(
                f"{message.type} debe estar identificado como mensaje de la central"
            )

    elif message.type in CITY_TYPES:
        if not isinstance(message.city_id, str) or not message.city_id:
            return _malformed(
                f"{message.type} debe incluir cityId"
            )

        if message.sender is not None:
            return _malformed(
                f"{message.type} no debe incluir sender"
            )

    else:
        #ACK, NACK, transfer y distance-table pueden usar alguna de las dos representaciones definidas en los contratos.
        if message.sender is None and message.city_id is None:
            return _malformed("El mensaje debe incluir sender o cityId")

        if message.sender is not None and message.sender != "central":
            return _malformed("sender debe ser 'central'")

    return None

#Comprueba cycleId en los mensajes donde es obligatorio.
def _validate_cycle(message: ProtocolEnvelope) -> ValidationResult | None:
    required = {
        "status-statement",
        "transfer",
        "demand-statement",
        "negotiation-proposal",
        "give",
        "take",
        "negotiation-report",
        "error",
    }

    if message.type in required:
        if not isinstance(message.cycle_id, str) or not message.cycle_id:
            return _malformed(f"{message.type} debe incluir cycleId")

    return None

#Valida status statement
def _validate_status_statement(message: ProtocolEnvelope) -> ValidationResult:
    data = message.data

    if not isinstance(data, dict):
        return _malformed("status-statement debe incluir data")

    energy = data.get("energy")

    if not isinstance(energy, dict):
        return _malformed("data.energy debe ser un objeto")

    for field in ("generationCapacity", "consumption", "generationCost"):
        if field not in energy:
            return _malformed(f"Falta data.energy.{field}")

        if not _is_number(energy[field]):
            return _malformed(f"data.energy.{field} debe ser numérico")

    if "validUntil" not in data:
        return _malformed("Falta data.validUntil")

    if not _is_datetime(data["validUntil"]):
        return _malformed("data.validUntil debe ser un datetime ISO 8601")

    return ValidationResult(valid=True)

#Validación de transferencia
def _validate_transfer(message: ProtocolEnvelope) -> ValidationResult:
    data = message.data

    if not isinstance(data, dict):
        return _malformed("transfer debe incluir data")

    if "quantity" not in data or not _is_number(data["quantity"]):
        return _malformed("data.quantity debe ser numérico")

    if "becauseOf" in data and not _is_uuid(data["becauseOf"]):
        return _malformed("data.becauseOf debe ser un UUID válido")

    return ValidationResult(valid=True)

#Valida el statement de demanda
def _validate_demand_statement(message: ProtocolEnvelope) -> ValidationResult:
    data = message.data

    if not isinstance(data, dict):
        return _malformed("demand-statement debe incluir data")

    balance = data.get("balance")

    if not isinstance(balance, dict):
        return _malformed("data.balance debe ser un objeto")

    for field in ("quantity", "valuePerKwh"):
        if field not in balance or not _is_number(balance[field]):
            return _malformed(f"data.balance.{field} debe ser numérico")

    return ValidationResult(valid=True)

#Valida la propuesta de negociación
def _validate_negotiation_proposal(
    message: ProtocolEnvelope,
) -> ValidationResult:
    data = message.data

    if not isinstance(data, dict):
        return _malformed("negotiation-proposal debe incluir data")

    if data.get("direction") not in {"take", "give"}:
        return _malformed("data.direction debe ser take o give")

    if not _is_positive_number(data.get("quantity")):
        return _malformed("data.quantity debe ser mayor que cero")

    if not _is_number(data.get("pricePerEnergy")):
        return _malformed("data.pricePerEnergy debe ser numérico")

    return ValidationResult(valid=True)

#Valida la confirmación
def _validate_confirmation(message: ProtocolEnvelope) -> ValidationResult:
    """Valida los mensajes give y take."""

    data = message.data

    if not isinstance(data, dict):
        return _malformed(f"{message.type} debe incluir data")

    if not _is_uuid(data.get("target")):
        return _malformed("data.target debe ser un UUID válido")

    if not _is_positive_number(data.get("energy")):
        return _malformed("data.energy debe ser mayor que cero")

    if not _is_number(data.get("pricePerEnergy")):
        return _malformed("data.pricePerEnergy debe ser numérico")

    return ValidationResult(valid=True)

#Valida el reporte de negociación
def _validate_negotiation_report(
    message: ProtocolEnvelope,
) -> ValidationResult:
    data = message.data

    if not isinstance(data, dict):
        return _malformed("negotiation-report debe incluir data")

    if not _is_number(data.get("budgetBalance")):
        return _malformed("data.budgetBalance debe ser numérico")

    if not _is_number(data.get("energyBalance")):
        return _malformed("data.energyBalance debe ser numérico")

    return ValidationResult(valid=True)

#Valida la request
def _validate_request(message: ProtocolEnvelope) -> ValidationResult:
    data = message.data

    if not isinstance(data, dict):
        return _malformed("request debe incluir data")

    if not isinstance(data.get("ask"), str) or not data["ask"]:
        return _malformed("data.ask debe ser un string no vacío")

    return ValidationResult(valid=True)

#Valida ACK
def _validate_ack(message: ProtocolEnvelope) -> ValidationResult:
    data = message.data

    if not isinstance(data, dict):
        return _malformed("ack debe incluir data")

    if not _is_uuid(data.get("target")):
        return _malformed("data.target debe ser un UUID válido")

    return ValidationResult(valid=True)

#Valida NACK
def _validate_nack(message: ProtocolEnvelope) -> ValidationResult:
    data = message.data
    raw = message.raw or {}

    if not isinstance(data, dict):
        return _malformed("nack debe incluir data")

    reason = raw.get("reason")
    code = raw.get("code")

    if reason not in NACK_CODES:
        return _malformed("reason de NACK desconocido")

    if code != NACK_CODES[reason]:
        return _malformed("reason y code del NACK no coinciden")

    if not _is_uuid(data.get("target")):
        return _malformed("data.target debe ser un UUID válido")

    if not isinstance(data.get("message"), str) or not data["message"]:
        return _malformed("data.message debe ser un string no vacío")

    if "cycleId" in data:
        if not isinstance(data["cycleId"], str) or not data["cycleId"]:
            return _malformed("data.cycleId debe ser un string no vacío")

    return ValidationResult(valid=True)

#Valida error
def _validate_error(message: ProtocolEnvelope) -> ValidationResult:
    data = message.data
    raw = message.raw or {}

    if not isinstance(data, dict):
        return _malformed("error debe incluir data")

    reason = raw.get("reason")
    code = raw.get("code")

    if reason not in ERROR_CODES:
        return _malformed("reason de error desconocido")

    if code != ERROR_CODES[reason]:
        return _malformed("reason y code del error no coinciden")

    if not _is_uuid(data.get("target")):
        return _malformed("data.target debe ser un UUID válido")

    if not isinstance(data.get("message"), str) or not data["message"]:
        return _malformed("data.message debe ser un string no vacío")

    if reason == "PRICE_ABOVE_CAP":
        if not _is_number(data.get("cap")):
            return _malformed("PRICE_ABOVE_CAP debe incluir data.cap")

    if reason == "OVER_CAPACITY":
        if not _is_number(data.get("spare")):
            return _malformed("OVER_CAPACITY debe incluir data.spare")

    return ValidationResult(valid=True)

#Valida distance table
def _validate_distance_table(message: ProtocolEnvelope) -> ValidationResult:
    data = message.data

    if not isinstance(data, dict):
        return _malformed("distance-table debe incluir data")

    distances = data.get("distances")

    if not isinstance(distances, dict):
        return _malformed("data.distances debe ser un objeto")

    for city, values in distances.items():
        if not isinstance(values, dict):
            return _malformed(f"Distancia para {city} debe ser un objeto")

        if not _is_number(values.get("distance")):
            return _malformed(f"{city}.distance debe ser numérico")

        if not _is_number(values.get("transportCost")):
            return _malformed(f"{city}.transportCost debe ser numérico")

        if not isinstance(values.get("enabled"), bool):
            return _malformed(f"{city}.enabled debe ser booleano")

    return ValidationResult(valid=True)

#Diccionario con las funciones de validación de contenido
CONTENT_VALIDATORS = {
    "status-statement": _validate_status_statement,
    "transfer": _validate_transfer,
    "demand-statement": _validate_demand_statement,
    "negotiation-proposal": _validate_negotiation_proposal,
    "give": _validate_confirmation,
    "take": _validate_confirmation,
    "negotiation-report": _validate_negotiation_report,
    "request": _validate_request,
    "ack": _validate_ack,
    "nack": _validate_nack,
    "error": _validate_error,
    "distance-table": _validate_distance_table,
}

#Función que valida un mensaje genérico ya parseado
def validate_message(message: ProtocolEnvelope) -> ValidationResult:
    #idpk y msgId representan conceptos distintos y nunca pueden ser iguales.
    if message.idpk == message.msg_id:
        return ValidationResult(
            valid=False,
            reason="IDPK_EQUALS_MSGID",
            code=422,
            message="idpk debe ser distinto de msgId",
        )

    #El type debe pertenecer al catálogo del protocolo.
    if message.type not in KNOWN_TYPES:
        return ValidationResult(
            valid=False,
            reason="UNKNOWN_TYPE",
            code=400,
            message=f"Tipo de mensaje desconocido: {message.type}",
        )

    #El timestamp debe traer zona horaria.
    if message.timestamp.tzinfo is None:
        return _malformed("timestamp debe incluir zona horaria")

    identity_result = _validate_identity(message)
    if identity_result is not None:
        return identity_result

    cycle_result = _validate_cycle(message)
    if cycle_result is not None:
        return cycle_result

    #Cada type tiene reglas propias para su contenido.
    validator = CONTENT_VALIDATORS[message.type]
    return validator(message)

#Función que parsea y valida un payload. MissingMsgIdError es separado porque un mensaje sin el msgId debe descartarse
def validate_payload(
    payload: dict,
) -> tuple[ProtocolEnvelope | None, ValidationResult]:
    try:
        message = parse_envelope(payload)
    except MissingMsgIdError:
        raise
    except EnvelopeParseError as exc:
        return None, _malformed(str(exc))

    return message, validate_message(message)