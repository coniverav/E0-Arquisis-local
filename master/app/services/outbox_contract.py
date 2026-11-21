"""Contrato puro de E3-025. No realiza I/O ni publica mensajes.

Los identificadores de una intención se generan al crear la fila, nunca
al reintentar el despacho. Se conservan los envelopes v3 sin alterar.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any, Mapping

EXECUTION_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
BROKER_TYPES = frozenset({"offer", "offer-updated", "offer-withdrawn", "negotiation-report"})


def validate_broker_envelope(envelope: Mapping[str, Any]) -> dict[str, Any]:
    """Verifica mínimos del envelope v3 y devuelve copia independiente."""
    if not isinstance(envelope, Mapping):
        raise ValueError("ENVELOPE_NOT_OBJECT")
    for key in ("msgId", "idpk", "type", "cycleId"):
        if not isinstance(envelope.get(key), str) or not envelope[key]:
            raise ValueError(f"ENVELOPE_MISSING_{key}")
    copy = deepcopy(dict(envelope))
    # Detecta datos no serializables antes de afectar la sesión DB.
    json.dumps(copy, allow_nan=False)
    return copy


def canonical_execution_input(value: Mapping[str, Any] | str) -> str:
    """Congela el input de StartExecution como texto JSON estable.

    Si se recibe texto JSON, conserva exactamente sus bytes (sin reserializar),
    después de validar que sea un objeto JSON.
    """
    if isinstance(value, str):
        parsed = json.loads(value, parse_constant=lambda item: (_ for _ in ()).throw(ValueError(f"INVALID_JSON_CONSTANT_{item}")))
        if not isinstance(parsed, dict):
            raise ValueError("EXECUTION_INPUT_NOT_OBJECT")
        return value
    if not isinstance(value, Mapping):
        raise ValueError("EXECUTION_INPUT_NOT_OBJECT")
    return json.dumps(dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def validate_execution_identity(idpk: str, execution_name: str | None = None) -> str:
    name = execution_name or idpk
    if not isinstance(idpk, str) or name != idpk or not EXECUTION_NAME_RE.fullmatch(name):
        raise ValueError("EXECUTION_NAME_MUST_EQUAL_IDPK")
    return name
