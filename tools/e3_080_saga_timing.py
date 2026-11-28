#!/usr/bin/env python3
"""E3-080: mide AD7 — ejecuciones de saga abiertas despues de closesAt + 60 s.

Objetivo del ADR-007: "no quedan ejecuciones abiertas 60 segundos despues de
`closesAt`". La consulta toma los ciclos ya cerrados, busca las ejecuciones de
saga asociadas por `cycle_id` y reporta dos tipos de desviacion:

    * `open_after_limit`: la ejecucion sigue en estado no terminal y ya paso el
      plazo `closesAt + max_age`.
    * `settled_late`: la ejecucion termino, pero su `terminal_at` cae despues de
      ese plazo.

La logica de evaluacion es pura y no toca la base; `load_dataset` es la unica
parte que consulta PostgreSQL. Asi el criterio se puede probar sin infraestructura.

Uso:
    python3 tools/e3_080_saga_timing.py --db-url "$DATABASE_URL"
    python3 tools/e3_080_saga_timing.py --db-url "$DATABASE_URL" --output markdown
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

TERMINAL_STATUSES = frozenset(
    {"SETTLED", "REJECTED", "COMPENSATED", "TIMED_OUT", "FAILED"}
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _seconds(delta: timedelta) -> float:
    return round(delta.total_seconds(), 1)


def evaluate(sagas, cycles, *, now: datetime, max_age_s: float = 60.0) -> dict:
    """Evalua el criterio AD7 sobre ejecuciones y ciclos ya cargados.

    `cycles` puede ser un mapeo `cycle_id -> closed_at` o una secuencia de
    objetos con atributos `cycle_id` y `closed_at`.
    """
    if isinstance(cycles, dict):
        closed_by_cycle = {key: _as_aware(value) for key, value in cycles.items()}
    else:
        closed_by_cycle = {
            cycle.cycle_id: _as_aware(cycle.closed_at) for cycle in cycles
        }
    closed_by_cycle = {
        cycle_id: closed_at
        for cycle_id, closed_at in closed_by_cycle.items()
        if closed_at is not None
    }

    now = _as_aware(now)
    limit = timedelta(seconds=max_age_s)

    deviations: list[dict] = []
    measured = 0
    open_after_limit = 0
    settled_late = 0
    settled_on_time = 0

    for saga in sagas:
        cycle_id = getattr(saga, "cycle_id", None)
        closed_at = closed_by_cycle.get(cycle_id)
        if closed_at is None:
            continue

        measured += 1
        deadline = closed_at + limit
        status = getattr(saga, "status", "UNKNOWN")
        terminal_at = _as_aware(getattr(saga, "terminal_at", None))
        role = getattr(saga, "role", None)
        idpk = getattr(saga, "idpk", None)

        if status in TERMINAL_STATUSES:
            if terminal_at is None:
                # Terminal sin sello temporal: no se puede demostrar el plazo.
                settled_late += 1
                deviations.append(
                    {
                        "kind": "terminal_without_timestamp",
                        "cycle_id": cycle_id,
                        "idpk": idpk,
                        "role": role,
                        "status": status,
                        "delay_s": None,
                        "cause": (
                            "estado terminal sin terminal_at; el plazo no es "
                            "demostrable"
                        ),
                    }
                )
            elif terminal_at > deadline:
                settled_late += 1
                deviations.append(
                    {
                        "kind": "settled_late",
                        "cycle_id": cycle_id,
                        "idpk": idpk,
                        "role": role,
                        "status": status,
                        "delay_s": _seconds(terminal_at - deadline),
                        "cause": (
                            "termino {delay} s despues de closesAt + {limit:g} s"
                        ).format(
                            delay=_seconds(terminal_at - deadline),
                            limit=max_age_s,
                        ),
                    }
                )
            else:
                settled_on_time += 1
        elif now > deadline:
            open_after_limit += 1
            deviations.append(
                {
                    "kind": "open_after_limit",
                    "cycle_id": cycle_id,
                    "idpk": idpk,
                    "role": role,
                    "status": status,
                    "delay_s": _seconds(now - deadline),
                    "cause": (
                        "sigue abierta en estado {status} {delay} s despues de "
                        "closesAt + {limit:g} s"
                    ).format(
                        status=status,
                        delay=_seconds(now - deadline),
                        limit=max_age_s,
                    ),
                }
            )

    # Un ciclo cerrado sin ninguna saga asociada no demuestra el criterio por si
    # solo: se reporta para no confundir "cero desviaciones" con "cero datos".
    cycles_without_sagas = sorted(
        cycle_id
        for cycle_id in closed_by_cycle
        if all(getattr(saga, "cycle_id", None) != cycle_id for saga in sagas)
    )

    all_ok = (
        measured > 0
        and open_after_limit == 0
        and settled_late == 0
    )

    return {
        "max_age_s": max_age_s,
        "cycles_closed": len(closed_by_cycle),
        "cycles_without_sagas": cycles_without_sagas,
        "sagas_measured": measured,
        "open_after_limit": open_after_limit,
        "settled_late": settled_late,
        "settled_on_time": settled_on_time,
        "deviations": deviations,
        "productive": measured > 0,
        "all_ok": all_ok,
    }


def load_dataset(db_url: str):
    """Carga ciclos cerrados y sus sagas desde PostgreSQL."""
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    from sqlmodel import Session, create_engine, select

    from master.app.models import Cycle, SagaExecution

    engine = create_engine(db_url)
    with Session(engine) as session:
        cycles = session.exec(select(Cycle).where(Cycle.closed_at != None)).all()  # noqa: E711
        sagas = session.exec(
            select(SagaExecution).where(SagaExecution.cycle_id != None)  # noqa: E711
        ).all()
    return cycles, sagas


def _format_markdown(summary: dict) -> str:
    lines = [
        f"## E3-080 · AD7: ejecuciones abiertas despues de closesAt + "
        f"{summary['max_age_s']:g} s",
        "",
        f"- **Ciclos cerrados:** {summary['cycles_closed']}",
        f"- **Sagas medidas:** {summary['sagas_measured']}",
        f"- **Abiertas fuera de plazo:** {summary['open_after_limit']}",
        f"- **Terminadas fuera de plazo:** {summary['settled_late']}",
        f"- **Terminadas en plazo:** {summary['settled_on_time']}",
    ]
    if summary["cycles_without_sagas"]:
        lines.append(
            "- **Ciclos cerrados sin sagas:** "
            + ", ".join(summary["cycles_without_sagas"])
        )
    if not summary["productive"]:
        lines += [
            "",
            "> Sin sagas asociadas a ciclos cerrados. La consulta no demuestra el "
            "criterio todavia: faltan datos, no hay cero desviaciones.",
        ]
    lines += ["", "| Tipo | Ciclo | idpk | Rol | Estado | Atraso (s) | Causa |",
              "| --- | --- | --- | --- | --- | --- | --- |"]
    for deviation in summary["deviations"]:
        lines.append(
            "| {kind} | {cycle} | {idpk} | {role} | {status} | {delay} | {cause} |".format(
                kind=deviation["kind"],
                cycle=deviation["cycle_id"],
                idpk=deviation["idpk"],
                role=deviation["role"],
                status=deviation["status"],
                delay=deviation["delay_s"],
                cause=deviation["cause"],
            )
        )
    if not summary["deviations"]:
        lines.append("| - | - | - | - | - | - | Sin desviaciones |")
    lines += [
        "",
        f"**Resultado:** {'PASS' if summary['all_ok'] else 'FAIL'}",
    ]
    return "\n".join(lines)


def _format_table(summary: dict) -> str:
    header = f"{'Tipo':<24} {'Ciclo':<14} {'Estado':<12} {'Atraso(s)':>10}"
    lines = [header, "-" * len(header)]
    for deviation in summary["deviations"]:
        lines.append(
            "{kind:<24} {cycle:<14} {status:<12} {delay:>10}".format(
                kind=deviation["kind"],
                cycle=str(deviation["cycle_id"])[:14],
                status=str(deviation["status"])[:12],
                delay=deviation["delay_s"],
            )
        )
    if not summary["deviations"]:
        lines.append("Sin desviaciones.")
    lines.append(
        "Sagas medidas: {measured} · abiertas fuera de plazo: {open} · "
        "tarde: {late} · {result}".format(
            measured=summary["sagas_measured"],
            open=summary["open_after_limit"],
            late=summary["settled_late"],
            result="PASS" if summary["all_ok"] else "FAIL",
        )
    )
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="E3-080: mide AD7 sobre ejecuciones de saga y ciclos cerrados"
    )
    parser.add_argument(
        "--db-url",
        default=os.getenv("DATABASE_URL"),
        help="URL SQLAlchemy a PostgreSQL (por defecto DATABASE_URL)",
    )
    parser.add_argument(
        "--max-age-s",
        type=float,
        default=60.0,
        help="Segundos permitidos despues de closesAt (por defecto 60, AD7)",
    )
    parser.add_argument(
        "--output", default="json", choices=["json", "markdown", "table"]
    )
    args = parser.parse_args(argv)

    if not args.db_url:
        parser.error("Falta --db-url o la variable DATABASE_URL")

    cycles, sagas = load_dataset(args.db_url)
    summary = evaluate(sagas, cycles, now=_now(), max_age_s=args.max_age_s)

    if args.output == "json":
        print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    elif args.output == "markdown":
        print(_format_markdown(summary))
    else:
        print(_format_table(summary))

    return 0 if summary["all_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
