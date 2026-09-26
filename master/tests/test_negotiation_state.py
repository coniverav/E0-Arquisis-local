import unittest
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from sqlmodel import Session

from app.database import (
    engine,
    run_migrations,
)
from app.models import (
    Cycle,
    Negotiation,
)
from app.services.negotiation_state import (
    NEGOTIATION_PENDING_PUBLICATION,
    NEGOTIATION_PROPOSED,
    NEGOTIATION_ACKNOWLEDGED,
    NEGOTIATION_CONFIRMED,
    NEGOTIATION_PAID,
    InvalidNegotiationTransition,
    transition_negotiation,
)


class NegotiationStateTests(
    unittest.TestCase
):

    @classmethod
    def setUpClass(cls):
        run_migrations()

    def setUp(self):
        self.connection = engine.connect()
        self.transaction = (
            self.connection.begin()
        )

        self.session = Session(
            bind=self.connection
        )

        now = datetime.now(
            timezone.utc
        )

        self.cycle_id = (
            f"state-{uuid4()}"
        )

        cycle = Cycle(
            cycle_id=self.cycle_id,
            opening_budget_balance=Decimal("0"),
            opening_energy_balance=Decimal("0"),
            budget_balance=Decimal("0"),
            energy_balance=Decimal("0"),
            last_sequence=0,
            status_payload={},
            created_at=now,
        )

        self.session.add(cycle)
        self.session.flush()

        self.negotiation = Negotiation(
            cycle_id=self.cycle_id,
            idpk=str(uuid4()),
            latest_msg_id=str(uuid4()),
            direction="take",
            requested_quantity=Decimal("10"),
            offered_price=Decimal("210"),
            status=(
                NEGOTIATION_PENDING_PUBLICATION
            ),
            created_at=now,
            updated_at=now,
        )

        self.session.add(
            self.negotiation
        )

        self.session.flush()

    def tearDown(self):
        self.session.close()
        self.transaction.rollback()
        self.connection.close()

    def test_complete_state_flow(self):

        transition_negotiation(
            self.session,
            self.negotiation,
            NEGOTIATION_PROPOSED,
        )

        self.assertIsNotNone(
            self.negotiation.deadline_at
        )

        transition_negotiation(
            self.session,
            self.negotiation,
            NEGOTIATION_ACKNOWLEDGED,
        )

        transition_negotiation(
            self.session,
            self.negotiation,
            NEGOTIATION_CONFIRMED,
        )

        transition_negotiation(
            self.session,
            self.negotiation,
            NEGOTIATION_PAID,
        )

        self.assertEqual(
            self.negotiation.status,
            NEGOTIATION_PAID,
        )

    def test_ack_does_not_mean_confirmed(self):

        transition_negotiation(
            self.session,
            self.negotiation,
            NEGOTIATION_PROPOSED,
        )

        transition_negotiation(
            self.session,
            self.negotiation,
            NEGOTIATION_ACKNOWLEDGED,
        )

        self.assertEqual(
            self.negotiation.status,
            NEGOTIATION_ACKNOWLEDGED,
        )

    def test_invalid_transition_is_rejected(
        self,
    ):

        with self.assertRaises(
            InvalidNegotiationTransition
        ):
            transition_negotiation(
                self.session,
                self.negotiation,
                NEGOTIATION_PAID,
            )

    def test_late_ack_does_not_regress_state(
        self,
    ):

        transition_negotiation(
            self.session,
            self.negotiation,
            NEGOTIATION_PROPOSED,
        )

        transition_negotiation(
            self.session,
            self.negotiation,
            NEGOTIATION_CONFIRMED,
        )

        changed = transition_negotiation(
            self.session,
            self.negotiation,
            NEGOTIATION_ACKNOWLEDGED,
        )

        self.assertFalse(changed)

        self.assertEqual(
            self.negotiation.status,
            NEGOTIATION_CONFIRMED,
        )


if __name__ == "__main__":
    unittest.main()