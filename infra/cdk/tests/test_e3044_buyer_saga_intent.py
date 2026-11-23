"""E3-044: contrato de input comprador sin JWT, aislado de PostgreSQL/AWS."""
import importlib.util
import json
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

SOURCE = (Path(__file__).resolve().parents[3] / "master" / "app" / "services"
          / "buyer_saga_intent.py")
spec = importlib.util.spec_from_file_location("buyer_saga_intent_e3044", SOURCE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class BuyerSagaIntentTests(unittest.TestCase):
    def setUp(self):
        now = datetime(2026, 11, 23, 12, tzinfo=timezone.utc)
        self.kwargs = dict(
            acceptance_idpk="accepted-uuid", bid_idpk="bid-uuid",
            bid_msg_id="bid-msg-uuid", cycle_id="cycle-uuid",
            offer_id="KLD-offer", seller_city_id="COR", quantity_kwh=7,
            amount_cr=Decimal("17.50"), swap_id="swap-uuid",
            accept_idempotency_key="accepted-uuid", swap_record_id=42,
            expires_at=now + timedelta(seconds=40),
            closes_at=now + timedelta(seconds=55), now=now,
        )

    def test_closed_asl_input_no_jwt_and_stable_identity(self):
        a = module.build_buyer_execution_input(**self.kwargs)
        b = module.build_buyer_execution_input(**self.kwargs)
        self.assertEqual(a, b)
        self.assertEqual((a["idpk"], a["sagaExecutionId"]), ("accepted-uuid", "accepted-uuid"))
        self.assertEqual(a["swapRecordId"], 42)
        self.assertEqual(a["swap"]["acceptIdempotencyKey"], "accepted-uuid")
        self.assertEqual(a["expectedClaims"]["take"], {"asset": "CR", "amount": "17.50"})
        self.assertNotIn("token", json.dumps(a).lower())
        self.assertNotIn("jwt", json.dumps(a).lower())
        self.assertEqual(a["timeoutAt"], "2026-11-23T12:01:40Z")

    def test_bad_input_fail_closed(self):
        cases = [
            {"accept_idempotency_key": "other"},
            {"quantity_kwh": 0},
            {"quantity_kwh": True},
            {"swap_record_id": None},
            {"amount_cr": Decimal("NaN")},
            {"amount_cr": Decimal("2.345")},
            {"expires_at": datetime(2026, 11, 23)},
            {"closes_at": datetime(2026, 11, 23)},
        ]
        for change in cases:
            with self.subTest(change=change):
                with self.assertRaises(ValueError):
                    module.build_buyer_execution_input(**(self.kwargs | change))

    def test_expired_jwt_still_gets_rejection_execution(self):
        old = self.kwargs["now"] - timedelta(minutes=1)
        value = module.build_buyer_execution_input(**(self.kwargs | {"expires_at": old}))
        self.assertGreater(value["timeoutAt"], value["swap"]["expiresAt"])
        self.assertEqual(value["timeoutAt"], "2026-11-23T12:01:00Z")


if __name__ == "__main__":
    unittest.main()
