"""E3-038: contrato JSON de inicio contra los JSONPath de la ASL seller.

Pruebas offline. No requieren Docker, AWS ni PostgreSQL.
"""
import importlib.util
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    "seller_saga_intent_under_test", ROOT / "master" / "app" / "services" / "seller_saga_intent.py",
)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class SellerSagaContractTest(unittest.TestCase):
    def setUp(self):
        self.input = module.build_seller_execution_input(
            bid_idpk="820e9739-a3e6-4a6d-b4f4-7193a47d34ab",
            bid_msg_id="dd282474-5818-4317-ab1c-e70d5e396ea6",
            cycle_id="cycle-E3-038", offer_id="KLD-cycle-E3-038",
            buyer_city_id="COR", quantity_kwh=40,
            give_amount=Decimal("40"), take_amount=Decimal("170.00"),
            wallet_idempotency_key="820e9739-a3e6-4a6d-b4f4-7193a47d34ab",
            market_closes_at=datetime(2026, 11, 25, 15, tzinfo=timezone.utc),
        )
        self.asl = json.loads((
            ROOT / "infra" / "cdk" / "energyshark_infra" / "asl" / "seller.asl.json"
        ).read_text())

    def _value(self, path):
        self.assertTrue(path.startswith("$."), path)
        value = self.input
        for name in path[2:].split("."):
            self.assertIn(name, value, path)
            value = value[name]
        return value

    def test_critical_state_paths_exist_at_start(self):
        states = self.asl["States"]
        # CancelProposedSwap se alcanza después de recibir la respuesta de Wallet:
        # $.proposalDetails no existe en el input inicial de StartExecution.
        for state_name in ("ReserveEnergy", "ProposeSwapAndWait"):
            parameters = states[state_name]["Parameters"]["Payload"]
            for key, value in parameters.items():
                if key.endswith(".$") and value.startswith("$."):
                    self._value(value)
        self.assertEqual(self.input["sagaExecutionId"], self.input["idpk"])
        self.assertEqual(self.input["quantity"], self.input["bid"]["quantityKwh"])

    def test_swap_id_is_created_after_proposal_callback(self):
        states = self.asl["States"]
        self.assertNotIn("proposalDetails", self.input)

        # Ambos caminos de propuesta construyen la estructura que usan
        # PublishBidAccepted, WaitForSellerTerminal y CancelProposedSwap.
        for state_name in ("UseProposalCallback", "UseProposalReconciliation"):
            producer = states[state_name]
            self.assertEqual(producer["Type"], "Pass")
            self.assertEqual(producer["ResultPath"], "$.proposalDetails")
            self.assertEqual(producer["Next"], "PublishBidAccepted")
            self.assertIn("swapId.$", producer["Parameters"])
            self.assertIn("expiresAt.$", producer["Parameters"])

        self.assertEqual(
            states["PublishBidAccepted"]["Parameters"]["Payload"]["swapId.$"],
            "$.proposalDetails.swapId",
        )
        self.assertEqual(
            states["CancelProposedSwap"]["Parameters"]["Payload"]["swapId.$"],
            "$.proposalDetails.swapId",
        )

    def test_task_timeouts_and_cancellation_key_are_stable(self):
        self.assertEqual(self.input["timeouts"]["proposalSeconds"], 30)
        self.assertEqual(self.input["timeouts"]["terminalSeconds"], 30)
        again = module.build_seller_execution_input(
            bid_idpk=self.input["idpk"],
            bid_msg_id=self.input["bid"]["msgId"],
            cycle_id=self.input["cycleId"], offer_id=self.input["offerId"],
            buyer_city_id="COR", quantity_kwh=40,
            give_amount=Decimal("40"), take_amount=Decimal("170.00"),
            wallet_idempotency_key=self.input["idpk"],
            market_closes_at=datetime(2026, 11, 25, 15, tzinfo=timezone.utc),
        )
        self.assertEqual(self.input, again)
        self.assertNotEqual(self.input["swap"]["cancelIdempotencyKey"], self.input["idpk"])
        self.assertNotIn("swapToken", json.dumps(self.input))


if __name__ == "__main__":
    unittest.main()
