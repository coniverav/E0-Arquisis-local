import json
import unittest
from pathlib import Path


ASL_DIR = (
    Path(__file__).resolve().parents[1]
    / "energyshark_infra"
    / "asl"
)


def _load(name: str) -> dict:
    return json.loads((ASL_DIR / name).read_text(encoding="utf-8"))


def _targets(state: dict) -> set[str]:
    targets = set()
    if "Next" in state:
        targets.add(state["Next"])
    if "Default" in state:
        targets.add(state["Default"])
    targets.update(choice["Next"] for choice in state.get("Choices", []))
    targets.update(handler["Next"] for handler in state.get("Catch", []))
    return targets


def _assert_closed_graph(test: unittest.TestCase, definition: dict) -> None:
    states = definition["States"]
    test.assertIn(definition["StartAt"], states)
    referenced = set().union(*(_targets(state) for state in states.values()))
    test.assertEqual(referenced - set(states), set())

    visited = set()
    pending = [definition["StartAt"]]
    while pending:
        name = pending.pop()
        if name in visited:
            continue
        visited.add(name)
        pending.extend(_targets(states[name]))
    test.assertEqual(set(states) - visited, set())


class SellerSagaDefinitionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = _load("manifest.json")
        self.seller = _load("seller.asl.json")

    def test_seller_is_standard_and_named_by_initiating_idpk(self) -> None:
        metadata = self.manifest["seller"]
        self.assertEqual(metadata["type"], "STANDARD")
        self.assertEqual(metadata["role"], "SELLER")
        self.assertEqual(metadata["executionNamePath"], "$.idpk")

    def test_seller_graph_is_closed_and_reachable(self) -> None:
        _assert_closed_graph(self, self.seller)

    def test_seller_has_callbacks_timeouts_reconciliation_and_compensation(self) -> None:
        states = self.seller["States"]
        for name in ("ProposeSwapAndWait", "WaitForSellerTerminal"):
            self.assertTrue(states[name]["Resource"].endswith("waitForTaskToken"))
            self.assertIn("TimeoutSecondsPath", states[name])
        for name in (
            "ReserveEnergy",
            "ReconcileProposal",
            "ReconcileSellerSwap",
            "CancelProposedSwap",
            "CompensateReservation",
            "FinalizeSellerSettled",
        ):
            self.assertIn(name, states)
        self.assertEqual(
            states["SellerReconciliationFailed"]["Type"],
            "Fail",
        )

    def test_wallet_retries_keep_stable_idempotency_keys(self) -> None:
        states = self.seller["States"]
        self.assertEqual(
            states["ProposeSwapAndWait"]["Parameters"]["Payload"][
                "idempotencyKey.$"
            ],
            "$.swap.idempotencyKey",
        )
        self.assertEqual(
            states["CancelProposedSwap"]["Parameters"]["Payload"][
                "idempotencyKey.$"
            ],
            "$.swap.cancelIdempotencyKey",
        )
        retry_errors = states["ProposeSwapAndWait"]["Retry"][0][
            "ErrorEquals"
        ]
        self.assertIn("Wallet.Retryable", retry_errors)
        self.assertIn("Wallet.RateLimited", retry_errors)


class BuyerSagaDefinitionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = _load("manifest.json")
        self.buyer = _load("buyer.asl.json")

    def test_buyer_is_standard_and_named_by_bid_accepted_idpk(self) -> None:
        metadata = self.manifest["buyer"]
        self.assertEqual(metadata["type"], "STANDARD")
        self.assertEqual(metadata["role"], "BUYER")
        self.assertEqual(metadata["executionNamePath"], "$.idpk")

    def test_buyer_graph_is_closed_and_reachable(self) -> None:
        _assert_closed_graph(self, self.buyer)

    def test_buyer_validates_token_deadline_and_reconciles_timeout(self) -> None:
        states = self.buyer["States"]
        self.assertEqual(self.buyer["StartAt"], "ValidateSwapToken")
        self.assertIn("CheckBuyerDeadline", states)
        self.assertIn("ReconcileBuyerSwap", states)
        acceptance = states["AcceptSwapAndWait"]
        self.assertTrue(acceptance["Resource"].endswith("waitForTaskToken"))
        self.assertIn("TimeoutSecondsPath", acceptance)
        timeout_targets = {
            handler["Next"]
            for handler in acceptance["Catch"]
            if "States.Timeout" in handler["ErrorEquals"]
        }
        self.assertEqual(timeout_targets, {"ReconcileBuyerSwap"})

    def test_buyer_retries_use_same_acceptance_key_and_hide_swap_token(self) -> None:
        acceptance = self.buyer["States"]["AcceptSwapAndWait"]
        payload = acceptance["Parameters"]["Payload"]
        self.assertEqual(
            payload["idempotencyKey.$"],
            "$.swap.acceptIdempotencyKey",
        )
        retry_errors = acceptance["Retry"][0]["ErrorEquals"]
        self.assertIn("Wallet.Retryable", retry_errors)
        self.assertIn("Wallet.RateLimited", retry_errors)
        self.assertNotIn("swapToken", json.dumps(self.buyer))

    def test_buyer_has_explicit_terminal_states(self) -> None:
        states = self.buyer["States"]
        for name in (
            "BuyerSettled",
            "BuyerRejected",
            "BuyerTimedOut",
            "BuyerReconciliationFailed",
            "BuyerFailed",
        ):
            self.assertIn(name, states)
        self.assertEqual(states["BuyerSettled"]["Type"], "Succeed")
        self.assertEqual(states["BuyerReconciliationFailed"]["Type"], "Fail")


if __name__ == "__main__":
    unittest.main()
