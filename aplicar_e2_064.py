#!/usr/bin/env python3
"""E2-064: integrar selección compradora sin cap de precio E1.

Ejecutar desde la raíz del repositorio tras extraer el ZIP allí.
Conserva versiones originales en .e2064_backup/.
Sin migración nueva; preserva modificaciones ajenas a los anchors esperados.
"""
from __future__ import annotations

import ast
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parent
MODULE = ROOT / 'master/app/services/autonomous_buy.py'
TEST_LEGACY = ROOT / 'master/tests/test_e2054_autonomous_buy.py'
TEST_NEW_SOURCE = ROOT / 'E2_064_nuevos/master/tests/test_e2064_buyer_policy.py'
TEST_NEW_TARGET = ROOT / 'master/tests/test_e2064_buyer_policy.py'
BACKUP = ROOT / '.e2064_backup'

# Reemplazos mínimos, todos deben concordar exactamente con la versión de develop.
MODULE_EDITS = [
    (
        '- un máximo de precio efectivo igual a `market.imbalancePrice`;\n'
        '- la capacidad de wallet (`CR available + creditLine`);',
        '- el precio efectivo (oferta + transporte oficial, sin cap de E1);\n'
        '- la capacidad de wallet (`CR available + creditLine`);',
    ),
    (
        'from app.models import Cycle, MarketBid\n',
        'from app.models import Cycle, MarketBid\n'
        'from app.services.budget_carryover import lock_city_ledger  # E2-064\n',
    ),
    (
        'BUDGET_CONSUMING_STATUSES = ("PENDING",)\n',
        '# E2-064: no inferir el precio de bids PENDING desde un feed mutable.\n',
    ),
    (
        '''def _imbalance_price(cycle: Cycle) -> Decimal | None:
    payload = cycle.status_payload or {}
    market = payload.get("market") if isinstance(payload, dict) else None
    if not isinstance(market, dict) or market.get("imbalancePrice") is None:
        return None
    price = _decimal(market.get("imbalancePrice"))
    return price if price >= 0 else None


''',
        '''def _effective_price(raw: Any) -> Decimal | None:
    """Precio verificable desde E2-036: nunca inventar CR/kWh faltantes."""
    if raw is None:
        return None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return value if value.is_finite() and value >= 0 else None


''',
    ),
    (
        '        "skipped_price": 0,\n',
        '        "skipped_price": 0,  # compatibilidad: solo precio inválido, NO cap E1\n'
        '        "skipped_pending": 0,\n',
    ),
    (
        '''    price_cap = _imbalance_price(cycle)
    if price_cap is None:
        logger.warning("Ciclo %s sin imbalancePrice; no se compra", cycle_id)
        return stats

''',
        '',
    ),
    (
        '''    active_bids = session.exec(
        select(MarketBid).where(
            MarketBid.cycle_id == cycle_id,
            MarketBid.direction == "OUTGOING",
            MarketBid.status.in_(ACTIVE_BID_STATUSES),
        )
    ).all()

    remaining_deficit = deficit - sum(bid.quantity for bid in active_bids)
    active_offers = {bid.offer_id for bid in active_bids}

    candidate_by_offer = {
        item.offerId: item for item in candidates.candidates
    }
    for bid in active_bids:
        if bid.status not in BUDGET_CONSUMING_STATUSES:
            continue
        candidate = candidate_by_offer.get(bid.offer_id)
        price = (
            _decimal(candidate.effectivePricePerEnergy)
            if candidate is not None
            and candidate.effectivePricePerEnergy is not None
            else price_cap
        )
        capacity -= Decimal(bid.quantity) * price

    if remaining_deficit <= 0:
        return stats
''',
        '''    # Serializar decisiones simultáneas de master/master2 después del HTTP.
    # E2-038 confirma cada bid y toma el mismo lock, sin llamadas HTTP bajo lock.
    lock_city_ledger(session)
    active_bids = session.exec(
        select(MarketBid).where(
            MarketBid.cycle_id == cycle_id,
            MarketBid.direction == "OUTGOING",
            MarketBid.status.in_(ACTIVE_BID_STATUSES),
        )
    ).all()

    # La cotización efectiva de un bid anterior NO está persistida en MarketBid.
    # El feed actual puede haber cambiado o desaparecido. Para no subestimar CR,
    # evitar nuevos bids mientras exista alguno pendiente de respuesta.
    # Se pueden crear varios bids en ESTE mismo tick, contabilizando sus costos.
    pending = [bid for bid in active_bids if bid.status == "PENDING"]
    if pending:
        stats["skipped_pending"] = len(pending)
        return stats

    remaining_deficit = deficit - sum(bid.quantity for bid in active_bids)
    active_offers = {bid.offer_id for bid in active_bids}
    if remaining_deficit <= 0:
        return stats
''',
    ),
    (
        '''        if candidate.effectivePricePerEnergy is None:
            continue

        price = _decimal(candidate.effectivePricePerEnergy)
        if price > price_cap:
            stats["skipped_price"] += 1
            continue
''',
        '''        # E2-036 calcula precio nominal + costo oficial de transporte,
        # incluyendo penalización de la ruta disabled. No comparar con
        # imbalancePrice ni con ningún límite E1 inventado.
        price = _effective_price(candidate.effectivePricePerEnergy)
        if price is None:
            stats["skipped_price"] += 1
            continue
''',
    ),
]

LEGACY_EDITS = [
    (
        'def test_autonomous_buy_covers_deficit_and_skips_expensive_offers(monkeypatch):',
        'def test_autonomous_buy_covers_deficit_without_e1_price_cap(monkeypatch):',
    ),
    (
        '''                _candidate(cycle_id, "O3", 400, "150", now),
                _candidate(cycle_id, "O1", 300, "50", now),
                _candidate(cycle_id, "O2", 400, "60", now),''',
        '''                # E2-037 entrega las candidatas ya ordenadas por precio efectivo.
                _candidate(cycle_id, "O1", 300, "50", now),
                _candidate(cycle_id, "O2", 400, "60", now),
                _candidate(cycle_id, "O3", 400, "150", now),''',
    ),
    (
        '        assert stats["skipped_price"] == 1',
        '        assert stats["skipped_price"] == 0',
    ),
]


def check_repo() -> None:
    required = [
        ROOT / 'master/app/services/offer_selection.py',
        ROOT / 'master/app/services/market_book.py',
        ROOT / 'master/app/services/buy_bids.py',
        ROOT / 'master/app/services/strategy_config.py',
        MODULE, TEST_LEGACY, TEST_NEW_SOURCE,
    ]
    absent = [str(p.relative_to(ROOT)) for p in required if not p.is_file()]
    if absent:
        raise RuntimeError('Faltan archivos/dependencias E2-036, 037, 054 o 062: ' + ', '.join(absent))


def patched(contents: str, edits: list[tuple[str, str]], relative: str) -> tuple[str, bool]:
    original = contents
    for old, new in edits:
        if old in contents and contents.count(old) == 1:
            contents = contents.replace(old, new, 1)
        elif new and new in contents:
            # Se permiten reruns, pero no mezclar parche parcial y antiguo.
            pass
        elif not new and old not in contents:
            # La eliminación de un bloque se verificará con una marca explícita.
            if '# E2-064' not in contents:
                raise RuntimeError(f'No se encontró bloque de E2-064 para eliminar: {relative}')
        else:
            raise RuntimeError(f'Archivo divergente: {relative}. No se modificarán archivos.')
    ast.parse(contents, filename=relative)
    return contents, contents != original


def save(path: Path, updated: str) -> None:
    rel = path.relative_to(ROOT)
    dest = BACKUP / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        shutil.copy2(path, dest)
    path.write_text(updated, encoding='utf-8')
    print('Modificado:', rel)


def main() -> None:
    check_repo()
    # Preflight completo antes de cambiar un byte.
    edits: list[tuple[Path, str, bool]] = []
    for path, replacements in ((MODULE, MODULE_EDITS), (TEST_LEGACY, LEGACY_EDITS)):
        contents = path.read_text(encoding='utf-8')
        updated, changed = patched(contents, replacements, str(path.relative_to(ROOT)))
        edits.append((path, updated, changed))
    new_test = TEST_NEW_SOURCE.read_text(encoding='utf-8')
    ast.parse(new_test, filename=str(TEST_NEW_SOURCE.relative_to(ROOT)))
    if TEST_NEW_TARGET.is_file() and TEST_NEW_TARGET.read_text(encoding='utf-8') != new_test:
        raise RuntimeError('El test E2-064 ya existe con contenido diferente: no sobrescribir.')
    for path, updated, changed in edits:
        if changed:
            save(path, updated)
        else:
            print('Ya instalado:', path.relative_to(ROOT))
    if not TEST_NEW_TARGET.exists():
        TEST_NEW_TARGET.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(TEST_NEW_SOURCE, TEST_NEW_TARGET)
        print('Creado:', TEST_NEW_TARGET.relative_to(ROOT))
    else:
        print('Ya instalado:', TEST_NEW_TARGET.relative_to(ROOT))
    print('\nE2-064 aplicado. No requiere migración nueva.')
    print('Pruebas: chmod +x probar_E2_064.sh && ./probar_E2_064.sh')
    print('Revisar con: git diff --stat && git diff --check')


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, SyntaxError) as exc:
        print(f'ERROR E2-064: {exc}', file=sys.stderr)
        sys.exit(1)
