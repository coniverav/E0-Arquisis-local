"""E3-035: caminos ASL locales y simulaciones de respuestas de Lambda.

No se ejecuta AWS Step Functions ni se contacta la wallet. Estas pruebas
validan el ruteo de los estados definidos en los ASL reales del repositorio.
Las pruebas de Lambda/HTTP y tokens del proyecto complementan este archivo.
"""
import copy
import json
import unittest
from pathlib import Path

ASL_DIR = Path(__file__).resolve().parents[1] / "energyshark_infra" / "asl"


def get_path(data, path):
    assert path.startswith("$."), path
    result = data
    for segment in path[2:].split("."):
        result = result[segment]
    return result


def assign_result(data, path, value):
    if path is None:
        return
    if path == "$":
        data.clear()
        data.update(value)
        return
    assert path.startswith("$."), path
    current = data
    names = path[2:].split(".")
    for name in names[:-1]:
        current = current.setdefault(name, {})
    current[names[-1]] = value


def select_target(state, error):
    for handler in state.get("Catch", []):
        if error in handler["ErrorEquals"] or "States.ALL" in handler["ErrorEquals"]:
            return handler["Next"]
    raise AssertionError(f"Error sin manejo: {error}")


def dry_run(asl_file, *, responses=None, errors=None):
    """Recorre Choice/Pass/Task con resultados simulados; no invoca recursos AWS."""
    definition = json.loads((ASL_DIR / asl_file).read_text(encoding="utf-8"))
    responses = responses or {}
    errors = errors or {}
    data = {"idpk": "bid-idpk-test", "cycleId": "test-cycle"}
    visited = []
    name = definition["StartAt"]
    for _ in range(50):
        visited.append(name)
        state = definition["States"][name]
        state_type = state["Type"]
        if state_type in ("Succeed", "Fail"):
            return visited, state_type, data
        if state_type == "Task":
            if name in errors:
                name = select_target(state, errors[name])
                continue
            assign_result(data, state.get("ResultPath", "$"), copy.deepcopy(responses.get(name, {})))
            name = state["Next"]
        elif state_type == "Pass":
            if "Result" in state:
                value = state["Result"]
            elif "Parameters" in state:
                value = {
                    (k[:-2] if k.endswith(".$") else k):
                    (get_path(data, v) if k.endswith(".$") else v)
                    for k, v in state["Parameters"].items()
                }
            else:
                value = {}
            assign_result(data, state.get("ResultPath", "$"), copy.deepcopy(value))
            name = state["Next"]
        elif state_type == "Choice":
            name = state.get("Default")
            for choice in state["Choices"]:
                try:
                    val = get_path(data, choice["Variable"])
                except (KeyError, TypeError):
                    continue
                if (("StringEquals" in choice and val == choice["StringEquals"])
                        or ("BooleanEquals" in choice and val is choice["BooleanEquals"])):
                    name = choice["Next"]
                    break
            if name is None:
                raise AssertionError("Choice sin ruta final")
        else:
            raise AssertionError(f"Tipo ASL no soportado en la simulación: {state_type}")
    raise AssertionError(f"La saga no terminó tras 50 estados: {visited}")


class SellerPathsTest(unittest.TestCase):
    def test_seller_settled_from_callbacks(self):
        path, outcome, _ = dry_run("seller.asl.json", responses={
            "ProposeSwapAndWait": {"outcome": "PROPOSED", "swapId": "s1", "expiresAt": "2099-01-01"},
            "WaitForSellerTerminal": {"outcome": "SETTLED"},
        })
        self.assertEqual(outcome, "Succeed")
        self.assertEqual(path[-1], "SellerSettled")
        self.assertIn("PublishBidAccepted", path)
        self.assertNotIn("CompensateReservation", path)

    def test_seller_timeout_reconciles_cancels_and_compensates(self):
        path, outcome, _ = dry_run("seller.asl.json", responses={
            "ProposeSwapAndWait": {"outcome": "PROPOSED", "swapId": "s2", "expiresAt": "2099-01-01"},
            "ReconcileSellerSwap": {"Payload": {"status": "PROPOSED"}},
            "CancelProposedSwap": {"Payload": {"status": "CANCELLED"}},
        }, errors={"WaitForSellerTerminal": "States.Timeout"})
        self.assertEqual(outcome, "Succeed")
        self.assertEqual(path[-1], "SellerCompensated")
        self.assertLess(path.index("ReconcileSellerSwap"), path.index("CancelProposedSwap"))
        self.assertLess(path.index("CancelProposedSwap"), path.index("CompensateReservation"))

    def test_seller_proposal_timeout_reconciles_rejected(self):
        path, outcome, _ = dry_run("seller.asl.json", responses={
            "ReconcileProposal": {"Payload": {"status": "REJECTED"}},
        }, errors={"ProposeSwapAndWait": "States.Timeout"})
        self.assertEqual(outcome, "Succeed")
        self.assertEqual(path[-1], "SellerCompensated")
        self.assertIn("ReconcileProposal", path)

    def test_seller_no_capacity_rejects_without_wallet_write(self):
        path, outcome, _ = dry_run("seller.asl.json", errors={
            "ReserveEnergy": "SellerInsufficientEnergy"})
        self.assertEqual(outcome, "Succeed")
        self.assertEqual(path[-1], "SellerRejected")
        self.assertNotIn("ProposeSwapAndWait", path)

    def test_seller_permanent_wallet_error_is_not_reconciled(self):
        path, outcome, _ = dry_run("seller.asl.json", errors={
            "ProposeSwapAndWait": "WalletIdempotencyMismatch"})
        self.assertEqual(outcome, "Fail")
        self.assertEqual(path[-1], "SellerFailed")
        self.assertNotIn("ReconcileProposal", path)


class BuyerPathsTest(unittest.TestCase):
    def test_buyer_settled(self):
        path, outcome, data = dry_run("buyer.asl.json", responses={
            "CheckBuyerDeadline": {"Payload": {"eligible": True}},
            "AcceptSwapAndWait": {"outcome": "SETTLED"},
        })
        self.assertEqual(outcome, "Succeed")
        self.assertEqual(path[-1], "BuyerSettled")
        self.assertEqual(data["terminal"]["status"], "SETTLED")

    def test_buyer_bad_token_never_calls_wallet(self):
        path, outcome, data = dry_run("buyer.asl.json", errors={
            "ValidateSwapToken": "BuyerExpiredToken"})
        self.assertEqual(outcome, "Succeed")
        self.assertEqual(path[-1], "BuyerRejected")
        self.assertEqual(data["terminal"]["reason"], "INVALID_SWAP_TOKEN")
        self.assertNotIn("AcceptSwapAndWait", path)

    def test_buyer_not_enough_time_rejects_before_wallet(self):
        path, outcome, _ = dry_run("buyer.asl.json", responses={
            "CheckBuyerDeadline": {"Payload": {"eligible": False}},
        })
        self.assertEqual(outcome, "Succeed")
        self.assertEqual(path[-1], "BuyerRejected")
        self.assertNotIn("AcceptSwapAndWait", path)

    def test_buyer_timeout_then_still_proposed(self):
        path, outcome, data = dry_run("buyer.asl.json", responses={
            "CheckBuyerDeadline": {"Payload": {"eligible": True}},
            "ReconcileBuyerSwap": {"Payload": {"status": "PROPOSED"}},
        }, errors={"AcceptSwapAndWait": "States.Timeout"})
        self.assertEqual(outcome, "Succeed")
        self.assertEqual(path[-1], "BuyerTimedOut")
        self.assertEqual(data["terminal"]["status"], "TIMED_OUT")
        self.assertIn("ReconcileBuyerSwap", path)

    def test_buyer_wallet_rejected_is_normal_terminal(self):
        path, outcome, _ = dry_run("buyer.asl.json", responses={
            "CheckBuyerDeadline": {"Payload": {"eligible": True}},
        }, errors={"AcceptSwapAndWait": "WalletRejected"})
        self.assertEqual(outcome, "Succeed")
        self.assertEqual(path[-1], "BuyerRejected")


class CallbackPolicyTest(unittest.TestCase):
    def test_all_callbacks_have_timeout_and_correct_key(self):
        for file, names in (
            ("seller.asl.json", ("ProposeSwapAndWait", "WaitForSellerTerminal")),
            ("buyer.asl.json", ("AcceptSwapAndWait",)),
        ):
            states = json.loads((ASL_DIR / file).read_text())["States"]
            for name in names:
                with self.subTest(file=file, task=name):
                    state = states[name]
                    self.assertIn("waitForTaskToken", state["Resource"])
                    self.assertTrue(state.get("TimeoutSecondsPath") or state.get("TimeoutSeconds"))
                    self.assertTrue(any("States.Timeout" in h["ErrorEquals"] for h in state["Catch"]))
        buyer = json.loads((ASL_DIR / "buyer.asl.json").read_text())["States"]
        self.assertEqual(buyer["AcceptSwapAndWait"]["Parameters"]["Payload"]["idempotencyKey.$"],
                         "$.swap.acceptIdempotencyKey")


if __name__ == "__main__":
    unittest.main()
