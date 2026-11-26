#!/usr/bin/env python3
"""E3-081: mide AD7 — reservas retenidas y objetivos del ADR-007 por ciclo.

Mide los tres objetivos de aceptacion del ADR-007 que dependen de datos locales:

    1. Ejecuciones abiertas despues de `closesAt + 60 s`        (meta: 0)
    2. Reservas `HOLD` retenidas al cierre o con saga terminal   (meta: 0)
    3. Inicios duplicados de saga por reentrega del mismo `idpk`  (meta: 0)

El resultado incluye los totales revisados para distinguir "cero desviaciones"
de "cero datos": un ciclo sin reservas ni sagas no demuestra el criterio.

La evaluacion es pura; `load_dataset` es la unica parte que consulta PostgreSQL.

Uso:
    python3 tools/e3_081_reservas.py --db-url "$DATABASE_URL"
    python3 tools/e3_081_reservas.py --db-url "$DATABASE_URL" --output markdown
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TOOLS_DIR = Path(__file__).resolve().parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

from e3_080_saga_timing import TERMINAL_STATUSES  # noqa: E402
from e3_080_saga_timing import evaluate as evaluate_saga_timing  # noqa: E402

HOLD = "HOLD"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _cycle_of_reservation(offer) -> str | None:
    return getattr(offer, "cycle_id", None)


def evaluate(
    *,
    reservations,
    offers_by_id: dict,
    sagas,
    cycles,
    now: datetime,
    max_age_s: float = 60.0,
) -> dict:
    """Evalua los tres objetivos del ADR-007 con datos ya cargados."""
    timing = evaluate_saga_timing(
        sagas, cycles, now=now, max_age_s=max_age_s
    )

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

    saga_by_idpk = {}
    duplicate_idpks: dict[str, int] = {}
    for saga in sagas:
        idpk = getattr(saga, "idpk", None)
        if idpk is None:
            continue
        duplicate_idpks[idpk] = duplicate_idpks.get(idpk, 0) + 1
        saga_by_idpk.setdefault(idpk, saga)

    retained = []
    reviewed = 0
    reviewed_closed = 0
    for reservation in reservations:
        status = getattr(reservation, "status", None)
        if status != HOLD:
            continue
        reviewed += 1
        offer = offers_by_id.get(getattr(reservation, "offer_id", None))
        cycle_id = _cycle_of_reservation(offer)
        if cycle_id not in closed_by_cycle:
            # La reserva pertenece a un ciclo abierto: aun no vence el plazo.
            continue
        reviewed_closed += 1

        saga = saga_by_idpk.get(getattr(reservation, "bid_idpk", None))
        reason = None
        if getattr(reservation, "released_at", None) is not None:
            continue
        if saga is None:
            reason = "Sin saga local que explique la reserva"
        elif getattr(saga, "status", None) in TERMINAL_STATUSES:
            reason = "Saga en estado {0} y la reserva sigue en HOLD".format(
                saga.status
            )
        else:
            deadline = closed_by_cycle[cycle_id] + limit
            if now > deadline:
                reason = "Saga abierta despues de closesAt + {0:g} s".format(
                    max_age_s
                )

        if reason:
            retained.append(
                {
                    "cycle_id": cycle_id,
                    "offer_id": getattr(reservation, "offer_id", None),
                    "bid_idpk": getattr(reservation, "bid_idpk", None),
                    "status": status,
                    "cause": reason,
                }
            )

    duplicates = {
        idpk: count for idpk, count in duplicate_idpks.items() if count > 1
    }

    objectives = [
        {
            "name": "Ejecuciones abiertas despues de closesAt + 60 s",
            "target": 0,
            "value": timing["open_after_limit"] + timing["settled_late"],
            "met": timing["open_after_limit"] == 0 and timing["settled_late"] == 0,
            "detail": (
                "{open} abiertas fuera de plazo, {late} terminadas tarde"
            ).format(
                open=timing["open_after_limit"], late=timing["settled_late"]
            ),
        },
        {
            "name": "Reservas HOLD retenidas al cierre",
            "target": 0,
            "value": len(retained),
            "met": not retained,
            "detail": "{0} reserva(s) HOLD sin liberar en ciclos cerrados".format(
                len(retained)
            ),
        },
        {
            "name": "Inicios duplicados por idpk",
            "target": 0,
            "value": len(duplicates),
            "met": not duplicates,
            "detail": "idpk repetidos: {0}".format(duplicates or "ninguno"),
        },
    ]

    productive = len(closed_by_cycle) > 0 and (
        timing["sagas_measured"] > 0 or reviewed_closed > 0
    )
    all_ok = productive and all(objective["met"] for objective in objectives)

    return {
        "max_age_s": max_age_s,
        "cycles_closed": len(closed_by_cycle),
        "sagas_measured": timing["sagas_measured"],
        "reservations_reviewed": reviewed,
        "reservations_reviewed_in_closed_cycles": reviewed_closed,
        "objectives": objectives,
        "retained_reservations": retained,
        "duplicate_idpks": duplicates,
        "timing": timing,
        "productive": productive,
        "all_ok": all_ok,
    }


def load_dataset(db_url: str):
    """Carga ciclos cerrados, sagas del ciclo y reservas con su oferta."""
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    from sqlmodel import Session, create_engine, select

    from master.app.models import Cycle, MarketOffer, OfferReservation, SagaExecution

    engine = create_engine(db_url)
    with Session(engine) as session:
        cycles = session.exec(select(Cycle).where(Cycle.closed_at != None)).all()  # noqa: E711
        sagas = session.exec(
            select(SagaExecution).where(SagaExecution.cycle_id != None)  # noqa: E711
        ).all()
        rows = session.exec(
            select(OfferReservation, MarketOffer)
            .join(MarketOffer, OfferReservation.offer_id == MarketOffer.offer_id)
        ).all()
    reservations = [row[0] for row in rows]
    offers_by_id = {row[1].offer_id: row[1] for row in rows}
    return cycles, sagas, reservations, offers_by_id


def _format_markdown(summary: dict) -> str:
    lines = [
        "## E3-081 · AD7: reservas retenidas y objetivos del ADR-007",
        "",
        f"- **Ciclos cerrados:** {summary['cycles_closed']}",
        f"- **Sagas medidas:** {summary['sagas_measured']}",
        f"- **Reservas HOLD revisadas:** {summary['reservations_reviewed']}",
    ]
    if not summary["productive"]:
        lines += [
            "",
            "> Sin sagas ni reservas en ciclos cerrados. La consulta no demuestra "
            "el criterio todavia: faltan datos, no hay cero desviaciones.",
        ]
    lines += ["", "| Objetivo | Meta | Medido | Cumple | Detalle |",
              "| --- | --- | --- | --- | --- |"]
    for objective in summary["objectives"]:
        lines.append(
            "| {name} | {target} | {value} | {met} | {detail} |".format(
                name=objective["name"],
                target=objective["target"],
                value=objective["value"],
                met="si" if objective["met"] else "no",
                detail=objective["detail"],
            )
        )
    if summary["retained_reservations"]:
        lines += ["", "### Reservas retenidas", ""]
        for item in summary["retained_reservations"]:
            lines.append(
                "- ciclo {cycle} · oferta {offer} · bid {bid}: {cause}".format(
                    cycle=item["cycle_id"],
                    offer=item["offer_id"],
                    bid=item["bid_idpk"],
                    cause=item["cause"],
                )
            )
    lines += [
        "",
        f"**Resultado:** {'PASS' if summary['all_ok'] else 'FAIL'}",
    ]
    return "\n".join(lines)


def _format_table(summary: dict) -> str:
    header = f"{'Objetivo':<52} {'Meta':>6} {'Medido':>7} {'Cumple':>7}"
    lines = [header, "-" * len(header)]
    for objective in summary["objectives"]:
        lines.append(
            "{name:<52} {target:>6} {value:>7} {met:>7}".format(
                name=objective["name"][:52],
                target=objective["target"],
                value=objective["value"],
                met="si" if objective["met"] else "no",
            )
        )
    lines.append(
        "Ciclos cerrados: {cycles} · sagas: {sagas} · reservas revisadas: "
        "{reservations} · {result}".format(
            cycles=summary["cycles_closed"],
            sagas=summary["sagas_measured"],
            reservations=summary["reservations_reviewed"],
            result="PASS" if summary["all_ok"] else "FAIL",
        )
    )
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="E3-081: mide reservas retenidas y objetivos ADR-007"
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

    cycles, sagas, reservations, offers_by_id = load_dataset(args.db_url)
    summary = evaluate(
        reservations=reservations,
        offers_by_id=offers_by_id,
        sagas=sagas,
        cycles=cycles,
        now=_now(),
        max_age_s=args.max_age_s,
    )

    if args.output == "json":
        print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    elif args.output == "markdown":
        print(_format_markdown(summary))
    else:
        print(_format_table(summary))

    return 0 if summary["all_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
