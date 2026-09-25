import unittest
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from sqlmodel import Session

from app.database import engine, run_migrations
from app.models import Cycle
from app.services.ledger import apply_ledger_effect
from app.services.negotiation_rules import (
    NegotiationRuleViolation,
    evaluate_negotiation_offer,
    maximum_sellable_energy,
    offer_price_cap,
    remaining_sellable_energy,
    settlement_price,
)


class NegotiationRulesTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        run_migrations()

    def setUp(self):
        self.connection = engine.connect()
        self.transaction = self.connection.begin()
        self.session = Session(
            bind=self.connection
        )

        self.cycle_id = (
            f"negotiation-rules-{uuid4()}"
        )

        self.cycle = Cycle(
            cycle_id=self.cycle_id,
            generation_capacity=Decimal("150.00"),
            consumption=Decimal("100.00"),
            generation_cost=Decimal("210.00"),
            opening_budget_balance=Decimal("0.00"),
            opening_energy_balance=Decimal("50.00"),
            budget_balance=Decimal("0.00"),
            energy_balance=Decimal("50.00"),
            last_sequence=0,
            status_payload={},
            created_at=datetime.now(
                timezone.utc
            ),
        )

        self.session.add(self.cycle)
        self.session.flush()

    def tearDown(self):
        self.session.close()
        self.transaction.rollback()
        self.connection.close()

    def test_price_cap_is_generation_cost_plus_five_percent(self):
        self.assertEqual(
            offer_price_cap(self.cycle),
            Decimal("220.50"),
        )

    def test_settlement_price_depends_on_direction(self):
        self.assertEqual(
            settlement_price(
                self.cycle,
                "take",
            ),
            Decimal("210.00"),
        )

        self.assertEqual(
            settlement_price(
                self.cycle,
                "give",
            ),
            Decimal("220.50"),
        )

    def test_maximum_sellable_energy_uses_generation_minus_consumption(self):
        self.assertEqual(
            maximum_sellable_energy(
                self.cycle
            ),
            Decimal("50.00"),
        )

        self.cycle.generation_capacity = (
            Decimal("80.00")
        )

        self.assertEqual(
            maximum_sellable_energy(
                self.cycle
            ),
            Decimal("0.00"),
        )

    def test_offer_at_cap_is_valid_and_above_cap_is_rejected(self):
        evaluation = evaluate_negotiation_offer(
            self.session,
            cycle=self.cycle,
            direction="take",
            quantity=Decimal("100.00"),
            price_per_energy=Decimal("220.50"),
        )

        self.assertEqual(
            evaluation.price_cap,
            Decimal("220.50"),
        )

        with self.assertRaises(
            NegotiationRuleViolation
        ) as context:
            evaluate_negotiation_offer(
                self.session,
                cycle=self.cycle,
                direction="take",
                quantity=Decimal("100.00"),
                price_per_energy=Decimal("220.51"),
            )

        error = context.exception

        self.assertEqual(
            error.reason,
            "PRICE_ABOVE_CAP",
        )

        self.assertEqual(
            error.code,
            422,
        )

        self.assertEqual(
            error.cap,
            Decimal("220.50"),
        )

    def test_give_at_spare_is_valid_and_over_capacity_is_rejected(self):
        evaluation = evaluate_negotiation_offer(
            self.session,
            cycle=self.cycle,
            direction="give",
            quantity=Decimal("50.00"),
            price_per_energy=Decimal("220.50"),
        )

        self.assertEqual(
            evaluation.spare,
            Decimal("50.00"),
        )

        with self.assertRaises(
            NegotiationRuleViolation
        ) as context:
            evaluate_negotiation_offer(
                self.session,
                cycle=self.cycle,
                direction="give",
                quantity=Decimal("50.01"),
                price_per_energy=Decimal("220.50"),
            )

        error = context.exception

        self.assertEqual(
            error.reason,
            "OVER_CAPACITY",
        )

        self.assertEqual(
            error.code,
            409,
        )

        self.assertEqual(
            error.spare,
            Decimal("50.00"),
        )

    def test_spare_subtracts_energy_already_sold(self):
        apply_ledger_effect(
            self.session,
            cycle_id=self.cycle_id,
            idpk=str(uuid4()),
            source_msg_id=str(uuid4()),
            operation_type="GIVE_CONFIRMED",
            budget_delta=Decimal("0.00"),
            energy_delta=Decimal("-20.00"),
            details={},
        )

        apply_ledger_effect(
            self.session,
            cycle_id=self.cycle_id,
            idpk=str(uuid4()),
            source_msg_id=str(uuid4()),
            operation_type="GIVE_CONFIRMED",
            budget_delta=Decimal("0.00"),
            energy_delta=Decimal("-15.00"),
            details={},
        )

        self.assertEqual(
            remaining_sellable_energy(
                self.session,
                self.cycle,
            ),
            Decimal("15.00"),
        )

    def test_take_does_not_use_give_capacity_limit(self):
        evaluation = evaluate_negotiation_offer(
            self.session,
            cycle=self.cycle,
            direction="take",
            quantity=Decimal("9999.00"),
            price_per_energy=Decimal("210.00"),
        )

        self.assertIsNone(
            evaluation.spare
        )


if __name__ == "__main__":
    unittest.main()
