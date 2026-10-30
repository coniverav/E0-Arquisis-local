"""E2-069: resumen de evidencia local sin dependencias externas."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path


def build(directory: Path) -> tuple[str, int]:
    rows = []
    for path in sorted(directory.glob('*.json')):
        record = json.loads(path.read_text(encoding='utf-8'))
        if record.get('mode') != 'SIMULACION_LOCAL_SIN_CENTRAL':
            raise ValueError(f'Evidencia sin marca local: {path}')
        if record.get('result') != 'PASS':
            raise ValueError(f'Evidencia no aprobada: {path}')
        rows.append(record)
    if not rows:
        raise ValueError('No se encontraron evidencias JSON de E2-069.')

    lines = [
        '# E2-069 — Evidencia de integración local AD4',
        '',
        '**Tipo:** SIMULACIÓN LOCAL, NO evidencia de Wallet Central real.',
        f'**Generado:** {datetime.now(timezone.utc).isoformat()}',
        f'**Escenarios aprobados y registrados:** {len(rows)}',
        '',
        '| Escenario | Oferta KWH | HOLD KWH | Reservados | Rechazados | available rechazos |',
        '|---|---:|---:|---:|---:|---|',
    ]
    for r in rows:
        available = ', '.join(map(str, r['available_rejections'])) or '—'
        lines.append(f"| {r['case']} | {r['capacity']} | {r['total_holds']} | "
                     f"{r['reserved_bids']} | {r['rejected_bids']} | {available} |")
    lines.extend([
        '',
        '## Interpretación',
        '',
        'Se probó el ingreso FastAPI con sesiones PostgreSQL concurrentes, '
        'el `SELECT FOR UPDATE` por oferta, las reservas persistentes, '
        'la correlación con SwapRecord y las respuestas `bid-rejected`.',
        'La prueba de la Wallet usa un **doble local**: permite comprobar '
        'que el backend no propone más KWH de los reservables, pero **no** '
        'demuestra ausencia de `INSUFFICIENT_ASSET` en la Wallet Central real.',
        'No se estableció conexión a la central ni se publicaron mensajes AMQP reales.',
        '',
        '## Pendiente de producción',
        '',
        'Repetir el caso con dos compradores reales separados 40 ms, '
        'verificar los eventos oficiales `swap-*` y confirmar que no exista '
        '`swap-rejected` de origen propio con razón `INSUFFICIENT_ASSET` en KWH.',
        '',
    ])
    return '\n'.join(lines), len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', default='datos_e2069')
    args = parser.parse_args()
    directory = Path(args.directory)
    content, count = build(directory)
    path = directory / 'RESUMEN_E2_069.md'
    path.write_text(content, encoding='utf-8')
    print(f'E2-069: {count} evidencias locales válidas → {path}')


if __name__ == '__main__':
    main()
