"""Constantes y utilidades compartidas por E2-066 (solo librería estándar)."""
from __future__ import annotations

import csv
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

SPANS = (0, 30, 60, 120, 240)
SELECTION_POLICIES = frozenset({"CHEAPEST_EFFECTIVE_PRICE", "MIN_DISTANCE"})
ACCEPTANCE_POLICIES = frozenset({"FIFO", "MAX_REMAINING", "BATCH"})
REGIMES = frozenset({"NORMAL", "HIGH_PRICE", "LOW_LIQUIDITY", "ROUTE_DISABLED", "LATE_OFFERS", "OTHER"})
PLAN_FIELDS = (
    "slot", "block", "position", "spanSeconds", "selectionPolicy", "acceptancePolicy",
    "cycleId", "observedRegime", "offersObserved", "liquidityRatio",
    "disabledRoutesShare", "marketWindowSeconds", "notes",
)


def write_atomic(path: str | Path, contents: str) -> None:
    """No deja un CSV/JSON parcialmente escrito si falla el proceso."""
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tempname = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=dest.parent,
                                prefix=".tmp_e2066_", delete=False) as handle:
            handle.write(contents)
            tempname = handle.name
        os.replace(tempname, dest)
    finally:
        if tempname and os.path.exists(tempname):
            os.unlink(tempname)


def csv_string(rows: list[dict], fields: tuple[str, ...] | list[str]) -> str:
    from io import StringIO
    buff = StringIO(newline="")
    writer = csv.DictWriter(buff, fieldnames=list(fields), lineterminator="\n", extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buff.getvalue()


def load_plan(path: str | Path) -> list[dict[str, str]]:
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or any(field not in reader.fieldnames for field in PLAN_FIELDS):
            raise ValueError("El plan no tiene todas las columnas E2-066")
        rows = [dict(row) for row in reader]
    seen_slots, seen_cycles = set(), set()
    for row in rows:
        try:
            slot, block, position, span = (int(row[k]) for k in ("slot", "block", "position", "spanSeconds"))
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError("Plan con índices o span inválidos") from exc
        if slot < 1 or block < 1 or position < 1 or span not in SPANS:
            raise ValueError("Plan con slot, bloque, posición o span inválidos")
        if slot in seen_slots:
            raise ValueError(f"Slot duplicado: {slot}")
        seen_slots.add(slot)
        if row["selectionPolicy"] not in SELECTION_POLICIES:
            raise ValueError("Selección no soportada")
        if row["acceptancePolicy"] not in ACCEPTANCE_POLICIES:
            raise ValueError("Aceptación no soportada")
        cycle = row["cycleId"].strip()
        if cycle:
            if cycle in seen_cycles:
                raise ValueError(f"cycleId duplicado en plan: {cycle}")
            seen_cycles.add(cycle)
    return rows


def load_metrics(path: str | Path) -> dict[str, dict]:
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = document.get("items") if isinstance(document, dict) else document
    if not isinstance(rows, list):
        raise ValueError("Métricas: se espera JSON con items[] o una lista")
    index = {}
    for item in rows:
        if not isinstance(item, dict) or not isinstance(item.get("cycleId"), str) or not item["cycleId"]:
            raise ValueError("Métricas contienen un item sin cycleId válido")
        if item["cycleId"] in index:
            raise ValueError(f"cycleId repetido en las métricas: {item['cycleId']}")
        index[item["cycleId"]] = item
    return index
