"""Pruebas puras, sin credenciales, servicios, Wallet ni PostgreSQL."""
from __future__ import annotations

from collections import Counter
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

from qa.e2066.analizar import analyze, report_markdown, summarize_regimes
from qa.e2066.comun import SPANS, PLAN_FIELDS, csv_string, load_metrics, load_plan, write_atomic
from qa.e2066.configurar_span import pending_payload
from qa.e2066.planificar import build_plan
from qa.e2066.recolectar import collect
from qa.e2066.registrar_ciclo import bind_cycle
from qa.e2066.simular import _transport, simulate_one, simulate_scenarios


class PlanTests(unittest.TestCase):
    def test_each_span_once_per_block_and_once_per_position_in_five_blocks(self):
        rows = build_plan(blocks=5, seed=2026)
        self.assertEqual(len(rows), 25)
        for block in range(1, 6):
            subset = [r for r in rows if int(r["block"]) == block]
            self.assertEqual({int(r["spanSeconds"]) for r in subset}, set(SPANS))
        for span in SPANS:
            subset = [r for r in rows if int(r["spanSeconds"]) == span]
            self.assertEqual({int(r["position"]) for r in subset}, set(range(1, 6)))
        self.assertEqual(rows, build_plan(blocks=5, seed=2026))

    def test_validates_plan_and_preserves_assignment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.csv"
            rows = build_plan(blocks=1)
            write_atomic(path, csv_string(rows, PLAN_FIELDS))
            loaded = load_plan(path)
            bind_cycle(loaded, slot=1, cycle_id="cycle-a", regime="NORMAL", window=600,
                       offers=4, liquidity="1.25", disabled_share="0.25")
            write_atomic(path, csv_string(loaded, PLAN_FIELDS))
            reread = load_plan(path)
            self.assertEqual(reread[0]["cycleId"], "cycle-a")
            self.assertEqual(reread[0]["liquidityRatio"], "1.25")
            with self.assertRaises(ValueError):
                bind_cycle(reread, slot=2, cycle_id="cycle-a")
            with self.assertRaises(ValueError):
                bind_cycle(reread, slot=1, cycle_id="cycle-other")
            with self.assertRaises(ValueError):
                bind_cycle(reread, slot=1, cycle_id="cycle-a", disabled_share="1.2")

    def test_prepares_pending_ad6_preserving_nonspan_fields(self):
        slot = build_plan(blocks=1)[0]
        snapshot = {
            "active": {"version": 8, "offerPriceMarkup": "1.3000",
                       "offerQuantityFraction": "0.7000", "bidAcceptancePolicy": "FIFO",
                       "offerSelectionPolicy": "MIN_DISTANCE", "spanSeconds": 60},
            "pending": {"version": 9, "offerPriceMarkup": "1.2600",
                        "offerQuantityFraction": "0.6000", "bidAcceptancePolicy": "BATCH",
                        "offerSelectionPolicy": "MIN_DISTANCE", "spanSeconds": 120},
        }
        payload = pending_payload(snapshot, slot)
        self.assertEqual(payload["expectedVersion"], 9)
        self.assertEqual(payload["spanSeconds"], int(slot["spanSeconds"]))
        self.assertEqual(payload["offerPriceMarkup"], "1.2600")
        self.assertEqual(payload["offerQuantityFraction"], "0.6000")
        self.assertEqual(payload["bidAcceptancePolicy"], "FIFO")
        self.assertEqual(payload["offerSelectionPolicy"], "CHEAPEST_EFFECTIVE_PRICE")


class CollectorTests(unittest.TestCase):
    def test_pagination_is_exhaustive(self):
        def fetch(params):
            items = [{"cycleId": f"cycle-{i}"} for i in range(5)]
            start = params["offset"]
            return {"total": 5, "items": items[start:start + params["limit"]]}
        result = collect(fetch, limit=2)
        self.assertEqual(result["total"], 5)
        self.assertEqual(len(result["items"]), 5)

    def test_duplicate_pagination_is_rejected(self):
        def fetch(params):
            return {"total": 2, "items": [{"cycleId": "same"}]}
        with self.assertRaises(RuntimeError):
            collect(fetch, limit=1)

    def test_shifting_totals_are_not_reported_as_complete(self):
        def fetch(params):
            if params["offset"] == 0:
                return {"total": 2, "items": [{"cycleId": "a"}]}
            return {"total": 3, "items": [{"cycleId": "b"}]}
        with self.assertRaises(RuntimeError):
            collect(fetch, limit=1)


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.plan = build_plan(blocks=1, seed=6)
        for i, row in enumerate(self.plan):
            row["cycleId"] = f"c{i}"

    def metric(self, row, *, real="60", baseline="100", **updates):
        m = {
            "cycleId": row["cycleId"], "spanSeconds": int(row["spanSeconds"]),
            "selectionPolicy": row["selectionPolicy"], "acceptancePolicy": row["acceptancePolicy"],
            "strategyVersion": 23, "status": "FINAL", "baselineSource": "SETTLEMENT",
            "missingTransportTrades": 0, "deficitKwh": 10, "boughtKwh": 8, "coveredKwh": 8,
            "coverageRate": "0.8", "rejectedBids": 1, "buyCost": "45.00",
            "transportCost": "5.00", "settlementCost": "10.00",
            "baselineCost": baseline, "realCost": real, "savingsVsBaseline": "40.00",
        }
        m.update(updates)
        return m

    def test_valid_final_costs_and_rate(self):
        metrics = {r["cycleId"]: self.metric(r) for r in self.plan}
        rows, byspan, contrast = analyze(self.plan, metrics)
        self.assertTrue(all(r["eligible"] == "YES" for r in rows))
        self.assertEqual(len(byspan), 5)
        self.assertEqual(len(contrast), 4)
        self.assertEqual(Decimal(rows[0]["savingsRate"]), Decimal("0.4"))
        self.assertEqual(Decimal(rows[0]["costPerDeficitKwh"]), Decimal("6"))
        self.assertIn("No atribuir causalidad", report_markdown(rows, byspan, contrast))
        self.plan[0]["observedRegime"] = "NORMAL"
        self.assertEqual(len(summarize_regimes(analyze(self.plan, metrics)[0])), 1)

    def test_pending_mismatch_and_zero_baseline_are_excluded(self):
        metrics = {r["cycleId"]: self.metric(r) for r in self.plan}
        metrics[self.plan[0]["cycleId"]]["status"] = "AWAITING_SETTLEMENT"
        metrics[self.plan[1]["cycleId"]]["spanSeconds"] = 999
        metrics[self.plan[2]["cycleId"]]["baselineCost"] = "0"
        metrics[self.plan[3]["cycleId"]]["baselineSource"] = "STATUS_ESTIMATE"
        rows, _, _ = analyze(self.plan, metrics)
        self.assertEqual([r["exclusionReason"] for r in rows[:4]], [
            "NOT_FINAL", "SPAN_MISMATCH_AD6", "ZERO_BASELINE", "BASELINE_NOT_CONFIRMED",
        ])
        self.assertEqual(rows[4]["eligible"], "YES")

    def test_missing_metrics_are_not_silently_zero(self):
        rows, _, _ = analyze(self.plan, {})
        self.assertTrue(all(row["exclusionReason"] == "METRICS_NOT_FOUND" for row in rows))

    def test_comparison_excludes_short_market_window(self):
        self.plan[0]["marketWindowSeconds"] = "60"
        self.plan[0]["spanSeconds"] = "240"
        m = self.metric(self.plan[0])
        rows, _, _ = analyze(self.plan, {self.plan[0]["cycleId"]: m})
        self.assertEqual(rows[0]["exclusionReason"], "SPAN_EXCEEDS_OBSERVED_BID_WINDOW")

    def test_metrics_loader_rejects_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metrics.json"
            write_atomic(path, json.dumps({"items": [{"cycleId": "a"}, {"cycleId": "a"}]}))
            with self.assertRaises(ValueError):
                load_metrics(path)


class SimulationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = json.loads(Path(__file__).parents[1].joinpath("escenarios_sinteticos.json").read_text(encoding="utf-8"))
        cls.scenarios = {s["id"]: s for s in cls.document["scenarios"]}

    def test_synthetic_matrix(self):
        results = simulate_scenarios(self.document)
        self.assertEqual(len(results), 6 * 2 * len(SPANS))
        self.assertTrue(all(row["evidenceType"] == "SYNTHETIC_COUNTERFACTUAL_NOT_REAL_RF06" for row in results))

    def test_no_e1_price_cap(self):
        result = simulate_one(self.scenarios["HIGH_PRICE"], span=0, policy="CHEAPEST_EFFECTIVE_PRICE")
        self.assertGreater(result["boughtKwh"], 0)
        self.assertLess(Decimal(result["estimatedSavings"]), 0)

    def test_disabled_route_penalty_is_used(self):
        offer = self.scenarios["ROUTE_DISABLED"]["offers"][0]
        _, eff = _transport(offer, self.scenarios["ROUTE_DISABLED"]["disabledRouteTransportCost"])
        self.assertEqual(eff, Decimal("220.00"))
        result = simulate_one(self.scenarios["ROUTE_DISABLED"], span=0, policy="CHEAPEST_EFFECTIVE_PRICE")
        self.assertIn("RD-COR", result["selectedOffers"])

    def test_early_closure_excludes_span_240(self):
        result = simulate_one(self.scenarios["EARLY_LAST_BID"], span=240, policy="MIN_DISTANCE")
        self.assertFalse(result["canBid"])
        self.assertEqual(result["reason"], "LAST_BID_PASSED")
        self.assertEqual(result["boughtKwh"], 0)

    def test_span_changes_offer_availability_but_same_market_fixture(self):
        base = simulate_one(self.scenarios["LATE_OFFERS"], span=0, policy="CHEAPEST_EFFECTIVE_PRICE")
        late = simulate_one(self.scenarios["LATE_OFFERS"], span=240, policy="CHEAPEST_EFFECTIVE_PRICE")
        self.assertIn("LO-MET", base["selectedOffers"])
        self.assertIn("LO-COR", late["selectedOffers"])
        self.assertNotEqual(base["estimatedTotalCost"], late["estimatedTotalCost"])


if __name__ == "__main__":
    unittest.main()
