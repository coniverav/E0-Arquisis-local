"""Plan secuencial de 5 spans, con balance de posición dentro de bloques.

Un ciclo real solo puede tener un span (AD6). El plan se ejecuta entre ciclos;
no cambia una estrategia durante el ciclo en curso.
"""
from __future__ import annotations

import argparse
import random
from collections import Counter

from .comun import SPANS, PLAN_FIELDS, csv_string, write_atomic


def build_plan(*, blocks: int = 5, seed: int = 2066,
               selection_policy: str = "CHEAPEST_EFFECTIVE_PRICE",
               acceptance_policy: str = "FIFO") -> list[dict[str, str]]:
    if type(blocks) is not int or blocks < 1 or blocks > 100:
        raise ValueError("blocks debe estar entre 1 y 100")
    if selection_policy not in ("CHEAPEST_EFFECTIVE_PRICE", "MIN_DISTANCE"):
        raise ValueError("Política compradora desconocida")
    if acceptance_policy not in ("FIFO", "MAX_REMAINING", "BATCH"):
        raise ValueError("Política vendedora desconocida")

    # 5 bloques consecutivos rotan cada span por las 5 posiciones. Los
    # siguientes bloques repiten el cuadrado latino con otro desplazamiento.
    shuffled = list(SPANS)
    random.Random(seed).shuffle(shuffled)
    rows = []
    for block in range(blocks):
        rotated = shuffled[block % len(SPANS):] + shuffled[:block % len(SPANS)]
        for position, span in enumerate(rotated, start=1):
            rows.append({
                "slot": str(len(rows) + 1), "block": str(block + 1),
                "position": str(position), "spanSeconds": str(span),
                "selectionPolicy": selection_policy,
                "acceptancePolicy": acceptance_policy,
                "cycleId": "", "observedRegime": "", "offersObserved": "",
                "liquidityRatio": "", "disabledRoutesShare": "",
                "marketWindowSeconds": "", "notes": "",
            })
    assert all(Counter(int(r["spanSeconds"]) for r in rows if int(r["block"]) == block)
               == Counter(SPANS) for block in range(1, blocks + 1))
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description="E2-066: plan experimental balanceado")
    parser.add_argument("--output", default="datos_e2066/plan_spans.csv")
    parser.add_argument("--blocks", type=int, default=5,
                        help="5 bloques = 25 ciclos (aprox. 8h20 si duran 20 min)")
    parser.add_argument("--seed", type=int, default=2066)
    parser.add_argument("--selection-policy", default="CHEAPEST_EFFECTIVE_PRICE",
                        choices=["CHEAPEST_EFFECTIVE_PRICE", "MIN_DISTANCE"])
    parser.add_argument("--acceptance-policy", default="FIFO",
                        choices=["FIFO", "MAX_REMAINING", "BATCH"])
    parser.add_argument("--force", action="store_true", help="Sobrescribe un plan previo")
    args = parser.parse_args(argv)
    from pathlib import Path
    path = Path(args.output)
    if path.exists() and not args.force:
        parser.error(f"Ya existe {path}; usar --force solo si realmente deseas reemplazarlo")
    rows = build_plan(blocks=args.blocks, seed=args.seed,
                      selection_policy=args.selection_policy,
                      acceptance_policy=args.acceptance_policy)
    write_atomic(path, csv_string(rows, PLAN_FIELDS))
    print(f"Plan guardado: {path} ({len(rows)} ciclos, spans {SPANS}).")
    print("Nota: cada edición AD6 entra en vigor en el siguiente ciclo.")


if __name__ == "__main__":
    main()
