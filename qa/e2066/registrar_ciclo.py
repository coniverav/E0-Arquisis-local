"""Asocia un cycleId real a un slot del plan sin reescribir otras asignaciones."""
from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation

from .comun import PLAN_FIELDS, REGIMES, csv_string, load_plan, write_atomic


def bind_cycle(plan: list[dict], *, slot: int, cycle_id: str, regime: str = "",
               offers: int | None = None, liquidity: str | None = None,
               disabled_share: str | None = None, window: int | None = None,
               notes: str | None = None) -> list[dict]:
    if not cycle_id or not cycle_id.strip() or len(cycle_id) > 128:
        raise ValueError("cycleId requerido, longitud máxima 128")
    if regime and regime not in REGIMES:
        raise ValueError(f"observedRegime debe ser uno de {sorted(REGIMES)}")
    if offers is not None and (type(offers) is not int or offers < 0):
        raise ValueError("offersObserved debe ser >= 0")
    if window is not None and (type(window) is not int or window < 0):
        raise ValueError("marketWindowSeconds debe ser >= 0")
    for name, raw, minval, maxval in (
        ("liquidityRatio", liquidity, Decimal(0), None),
        ("disabledRoutesShare", disabled_share, Decimal(0), Decimal(1)),
    ):
        if raw is None:
            continue
        try:
            v = Decimal(str(raw))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise ValueError(f"{name} numérico requerido") from exc
        if not v.is_finite() or v < minval or (maxval is not None and v > maxval):
            raise ValueError(f"{name} fuera de rango")
    if any(row["cycleId"] == cycle_id and int(row["slot"]) != slot for row in plan):
        raise ValueError("cycleId ya asociado a otro slot")
    row = next((r for r in plan if int(r["slot"]) == slot), None)
    if row is None:
        raise ValueError(f"Slot {slot} no existe")
    if row["cycleId"] and row["cycleId"] != cycle_id:
        raise ValueError(f"Slot {slot} ya tiene cycleId {row['cycleId']}")
    row["cycleId"] = cycle_id
    if regime:
        row["observedRegime"] = regime
    if offers is not None:
        row["offersObserved"] = str(offers)
    if liquidity is not None:
        row["liquidityRatio"] = str(Decimal(str(liquidity)))
    if disabled_share is not None:
        row["disabledRoutesShare"] = str(Decimal(str(disabled_share)))
    if window is not None:
        row["marketWindowSeconds"] = str(window)
    if notes is not None:
        row["notes"] = notes
    return plan


def main(argv=None):
    parser = argparse.ArgumentParser(description="E2-066: registrar ciclo observado")
    parser.add_argument("--plan", default="datos_e2066/plan_spans.csv")
    parser.add_argument("--slot", required=True, type=int)
    parser.add_argument("--cycle-id", required=True)
    parser.add_argument("--regime", default="", choices=["", *sorted(REGIMES)])
    parser.add_argument("--offers", type=int)
    parser.add_argument("--liquidity")
    parser.add_argument("--disabled-share")
    parser.add_argument("--window", type=int)
    parser.add_argument("--notes")
    args = parser.parse_args(argv)
    plan = load_plan(args.plan)
    bind_cycle(plan, slot=args.slot, cycle_id=args.cycle_id, regime=args.regime,
               offers=args.offers, liquidity=args.liquidity,
               disabled_share=args.disabled_share, window=args.window, notes=args.notes)
    write_atomic(args.plan, csv_string(plan, PLAN_FIELDS))
    print(f"Registrado slot={args.slot}, cycleId={args.cycle_id}.")


if __name__ == "__main__":
    main()
