"""Carga de los contratos v3.0.2 vendorizados para las pruebas.

Los schemas son copias exactas del tag oficial (ver
`contracts/v3/PROVENANCE.md`). Este módulo no redefine el contrato: sólo lo
carga con referencias locales, sin acceso a red.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012


ROOT = Path(__file__).resolve().parent / "contracts" / "v3"
SCHEMAS = ROOT / "schemas"
EXAMPLES = ROOT / "examples"
SOURCE_SHA256 = ROOT / "SOURCE_SHA256.json"


@lru_cache(maxsize=1)
def _load() -> tuple[dict[str, dict[str, Any]], Registry]:
    schemas: dict[str, dict[str, Any]] = {}
    registry = Registry()

    for path in sorted(SCHEMAS.rglob("*.schema.json")):
        schema = json.loads(path.read_text(encoding="utf-8"))
        relative = path.relative_to(SCHEMAS).as_posix()
        schemas[relative] = schema
        registry = registry.with_resource(
            schema["$id"],
            Resource.from_contents(schema, default_specification=DRAFT202012),
        )

    return schemas, registry


def validate(kind: str, payload: dict[str, Any]) -> None:
    """Valida `payload` contra el schema `kind` (p. ej. `amqp/offer`)."""

    schemas, registry = _load()
    key = f"{kind}.schema.json"
    if key not in schemas:
        raise KeyError(f"schema no vendorizado: {key}")

    Draft202012Validator(
        schemas[key],
        registry=registry,
        format_checker=FormatChecker(),
    ).validate(payload)


def example(name: str) -> dict[str, Any]:
    """Carga un ejemplo válido vendorizado, sin modificar."""

    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


def schema_hashes() -> dict[str, str]:
    return json.loads(SOURCE_SHA256.read_text(encoding="utf-8"))
