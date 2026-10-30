"""Utilidades y contratos locales de E2-067; Python estándar únicamente."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re

from qa.e2066.comun import load_plan, write_atomic, csv_string

DATA_DIR = Path('datos_e2067')
PHASES = ('early', 'final')
SAFE_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$')
REAL_METRIC_FIELDS = (
    'cycleId', 'strategyVersion', 'spanSeconds', 'selectionPolicy',
    'acceptancePolicy', 'deficitKwh', 'boughtKwh', 'coveredKwh',
    'coverageRate', 'rejectedBids', 'rejectedIncomingBids',
    'buyCost', 'transportCost', 'settlementCost', 'realCost',
    'baselineCost', 'baselineSource', 'savingsVsBaseline',
    'missingTransportTrades', 'status', 'createdAt', 'updatedAt',
)


def cycle_slug(cycle_id: str) -> str:
    if not isinstance(cycle_id, str) or not SAFE_ID.fullmatch(cycle_id):
        raise ValueError('cycleId inseguro para ruta local; usar solo letras, números, _, . y -')
    return cycle_id


def evidence_path(data_dir: Path, slot: int, cycle_id: str, phase: str) -> Path:
    if phase not in PHASES or slot < 1:
        raise ValueError('slot o phase inválido')
    return data_dir / 'evidencias' / f'{slot:03d}_{cycle_slug(cycle_id)}' / f'{phase}.json'


def wallet_path(data_dir: Path, cycle_id: str) -> Path:
    return data_dir / 'wallet' / f'{cycle_slug(cycle_id)}.json'


def read_json(path: Path) -> dict:
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        raise ValueError(f'JSON inválido: {path}')
    return data


def save_json(path: Path, data: dict, *, replace: bool = False) -> None:
    if path.exists() and not replace:
        raise FileExistsError(f'Ya existe {path}. Usar --refresh para sobrescribir conscientemente.')
    write_atomic(path, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + '\n')


def row_for_slot(plan: list[dict[str, str]], slot: int) -> dict[str, str]:
    row = next((r for r in plan if int(r['slot']) == slot), None)
    if row is None:
        raise ValueError(f'Slot {slot} no existe')
    if not row['cycleId'].strip():
        raise ValueError(f'Slot {slot} sin cycleId; usar qa.e2066.registrar_ciclo primero')
    cycle_slug(row['cycleId'])
    return row


def decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
        return parsed if parsed.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def aware_datetime(value: str | None) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        date = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return date.astimezone(timezone.utc) if date.tzinfo else None
    except ValueError:
        return None


def keep(obj: dict, fields: tuple[str, ...]) -> dict:
    return {key: obj.get(key) for key in fields if key in obj}
