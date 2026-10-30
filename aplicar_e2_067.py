#!/usr/bin/env python3
"""E2-067: instala herramientas QA para documentar ciclos reales de RF06.

Solo añade QA + documentación; no modifica código del backend, Wallet,
RabbitMQ, migraciones o configuraciones. No reemplaza diferencias sin --force.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil

HERE = Path(__file__).resolve().parent
PAYLOAD = HERE / 'e2067_payload'


def install(repo: Path, *, force: bool = False) -> tuple[int, int]:
    if not (repo / 'docker-compose.yml').is_file() or not (repo / 'master/app/models.py').is_file():
        raise ValueError('Ejecutar desde la raíz de EnergyShark (con docker-compose.yml y master/)')
    if not (repo / 'qa/e2066/comun.py').is_file():
        raise ValueError('E2-067 depende de E2-066. Ejecutar primero python3 aplicar_e2_066.py')
    if 'class CycleStrategyMetric' not in (repo / 'master/app/models.py').read_text(encoding='utf-8'):
        raise ValueError('E2-067 depende de métricas E2-065. Instalar/corregir E2-065 antes.')
    candidates = [path for path in PAYLOAD.rglob('*') if path.is_file()
                  and '__pycache__' not in path.parts and path.suffix != '.pyc']
    if not candidates:
        raise ValueError('Falta e2067_payload/. Descomprime el ZIP completo en la raíz del repositorio.')
    mismatches = [str(p.relative_to(PAYLOAD)) for p in candidates
                  if (repo / p.relative_to(PAYLOAD)).is_file()
                  and (repo / p.relative_to(PAYLOAD)).read_bytes() != p.read_bytes()]
    if mismatches and not force:
        raise ValueError('Archivos distintos; no se modificaron: ' + ', '.join(mismatches)
                         + '. Usar --force solo si revisaste los cambios.')
    installed = skipped = 0
    for source in candidates:
        rel = source.relative_to(PAYLOAD)
        dest = repo / rel
        if dest.is_file() and dest.read_bytes() == source.read_bytes():
            skipped += 1
            continue
        if dest.exists():
            backup = repo / '.e2067_backup' / rel
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dest, backup)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        installed += 1
    return installed, skipped


def main() -> None:
    parser = argparse.ArgumentParser(description='Instalar E2-067, QA/Data RF06')
    parser.add_argument('--force', action='store_true', help='Sobrescribir diferencias, respaldando originales')
    args = parser.parse_args()
    try:
        installed, skipped = install(Path.cwd(), force=args.force)
    except ValueError as exc:
        parser.error(str(exc))
    print(f'E2-067 instalado: {installed} nuevos/actualizados; {skipped} intactos.')
    print('Solo QA / datos locales / documentación; no se alteró la lógica productiva.')
    print('Pruebas: chmod +x probar_E2_067.sh && ./probar_E2_067.sh')


if __name__ == '__main__':
    main()
