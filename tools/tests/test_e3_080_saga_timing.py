"""E3-080: contratos del criterio AD7 sobre sagas y ciclos cerrados."""

import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

MODULE_PATH = Path(__file__).resolve().parents[1] / "e3_080_saga_timing.py"
spec = importlib.util.spec_from_file_location("e3_080_saga_timing", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc)


def saga(idpk="bid-1", status="SETTLED", closed_after_s=10, cycle="c-1", role="SELLER",
         terminal=True):
    return SimpleNamespace(
        idpk=idpk,
        cycle_id=cycle,
        role=role,
        status=status,
        terminal_at=NOW - timedelta(seconds=closed_after_s) if terminal else None,
    )


def cycles(closed_s=120, cycle="c-1"):
    return {cycle: NOW - timedelta(seconds=closed_s)}


class EvaluateTest(unittest.TestCase):
    def test_saga_settled_inside_window_passes(self) -> None:
        # Ciclo cerrado hace 120 s; la saga termino hace 100 s => 20 s despues.
        summary = module.evaluate(
            [saga(closed_after_s=100)], cycles(), now=NOW, max_age_s=60
        )
        self.assertTrue(summary["all_ok"])
        self.assertEqual(summary["settled_on_time"], 1)
        self.assertEqual(summary["deviations"], [])

    def test_saga_settled_late_is_deviation(self) -> None:
        # Termino hace 30 s => 90 s despues del cierre; 30 s fuera del plazo.
        summary = module.evaluate(
            [saga(closed_after_s=30)], cycles(), now=NOW, max_age_s=60
        )
        self.assertFalse(summary["all_ok"])
        self.assertEqual(summary["settled_late"], 1)
        deviation = summary["deviations"][0]
        self.assertEqual(deviation["kind"], "settled_late")
        self.assertEqual(deviation["cycle_id"], "c-1")
        self.assertAlmostEqual(deviation["delay_s"], 30.0)

    def test_saga_still_open_past_limit_is_deviation(self) -> None:
        summary = module.evaluate(
            [saga(status="WAITING", terminal=False, closed_after_s=0)],
            cycles(closed_s=120),
            now=NOW,
            max_age_s=60,
        )
        self.assertFalse(summary["all_ok"])
        self.assertEqual(summary["open_after_limit"], 1)
        self.assertEqual(summary["deviations"][0]["kind"], "open_after_limit")

    def test_saga_in_open_cycle_is_not_measured(self) -> None:
        summary = module.evaluate(
            [saga()], {"c-1": None}, now=NOW, max_age_s=60
        )
        self.assertEqual(summary["sagas_measured"], 0)
        self.assertFalse(summary["all_ok"])
        self.assertFalse(summary["productive"])

    def test_no_data_is_not_a_pass(self) -> None:
        summary = module.evaluate([], cycles(), now=NOW, max_age_s=60)
        self.assertFalse(summary["all_ok"])
        self.assertFalse(summary["productive"])
        self.assertEqual(summary["cycles_without_sagas"], ["c-1"])

    def test_terminal_without_timestamp_cannot_prove_deadline(self) -> None:
        summary = module.evaluate(
            [
                SimpleNamespace(
                    idpk="b",
                    cycle_id="c-1",
                    role="BUYER",
                    status="SETTLED",
                    terminal_at=None,
                )
            ],
            cycles(),
            now=NOW,
            max_age_s=60,
        )
        self.assertFalse(summary["all_ok"])
        self.assertEqual(
            summary["deviations"][0]["kind"], "terminal_without_timestamp"
        )

    def test_cycles_accepts_model_objects(self) -> None:
        cycle = SimpleNamespace(cycle_id="c-1", closed_at=NOW - timedelta(seconds=120))
        summary = module.evaluate(
            [saga(closed_after_s=110)], [cycle], now=NOW, max_age_s=60
        )
        self.assertTrue(summary["all_ok"])


if __name__ == "__main__":
    unittest.main()
