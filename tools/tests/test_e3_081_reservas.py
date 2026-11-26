"""E3-081: contratos de reservas retenidas y objetivos del ADR-007."""

import importlib.util
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

TOOLS_DIR = Path(__file__).resolve().parents[1]
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

MODULE_PATH = TOOLS_DIR / "e3_081_reservas.py"
spec = importlib.util.spec_from_file_location("e3_081_reservas", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc)


def offer(offer_id="o-1", cycle="c-1", quantity=10):
    return SimpleNamespace(
        offer_id=offer_id, cycle_id=cycle, is_local=True,
        initial_quantity=quantity,
    )


def reservation(offer_id="o-1", bid="bid-1", status="HOLD", released_at=None):
    return SimpleNamespace(
        offer_id=offer_id, bid_idpk=bid, status=status, released_at=released_at,
    )


def saga(idpk="bid-1", status="SETTLED", settled_s_ago=100, cycle="c-1"):
    return SimpleNamespace(
        idpk=idpk, cycle_id=cycle, role="SELLER", status=status,
        terminal_at=NOW - timedelta(seconds=settled_s_ago),
    )


def cycles(closed_s=120):
    return {"c-1": NOW - timedelta(seconds=closed_s)}


class EvaluateTest(unittest.TestCase):
    def test_clean_cycle_meets_all_objectives(self) -> None:
        summary = module.evaluate(
            reservations=[reservation(status="RELEASED",
                                      released_at=NOW - timedelta(seconds=90))],
            offers_by_id={"o-1": offer()},
            sagas=[saga()],
            cycles=cycles(),
            now=NOW,
            max_age_s=60,
        )
        self.assertTrue(summary["productive"])
        self.assertTrue(summary["all_ok"])
        self.assertEqual([o["value"] for o in summary["objectives"]], [0, 0, 0])

    def test_hold_with_terminal_saga_is_retained(self) -> None:
        summary = module.evaluate(
            reservations=[reservation()],
            offers_by_id={"o-1": offer()},
            sagas=[saga(status="COMPENSATED")],
            cycles=cycles(),
            now=NOW,
            max_age_s=60,
        )
        self.assertFalse(summary["all_ok"])
        self.assertEqual(summary["retained_reservations"][0]["cycle_id"], "c-1")
        self.assertIn("HOLD", summary["retained_reservations"][0]["cause"])
        self.assertEqual(summary["objectives"][1]["value"], 1)

    def test_hold_in_open_cycle_is_not_judged(self) -> None:
        summary = module.evaluate(
            reservations=[reservation()],
            offers_by_id={"o-1": offer()},
            sagas=[saga()],
            cycles={"c-1": None},
            now=NOW,
            max_age_s=60,
        )
        self.assertEqual(summary["retained_reservations"], [])
        self.assertFalse(summary["productive"])

    def test_duplicate_saga_start_is_deviation(self) -> None:
        summary = module.evaluate(
            reservations=[],
            offers_by_id={"o-1": offer()},
            sagas=[saga(), saga()],
            cycles=cycles(),
            now=NOW,
            max_age_s=60,
        )
        self.assertFalse(summary["all_ok"])
        self.assertEqual(summary["duplicate_idpks"], {"bid-1": 2})
        self.assertEqual(summary["objectives"][2]["value"], 1)

    def test_no_data_is_not_a_pass(self) -> None:
        summary = module.evaluate(
            reservations=[],
            offers_by_id={},
            sagas=[],
            cycles=cycles(),
            now=NOW,
            max_age_s=60,
        )
        self.assertFalse(summary["productive"])
        self.assertFalse(summary["all_ok"])


if __name__ == "__main__":
    unittest.main()
