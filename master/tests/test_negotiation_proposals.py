import unittest
from datetime import (
    datetime,
    timedelta,
    timezone,
)
from decimal import Decimal
from uuid import uuid4

from sqlmodel import Session, select

from app.database import (
    engine,
    run_migrations,
)
from app.models import (
    Cycle,
    Negotiation,
    OutboundMessage,
)
from app.services.negotiation_proposals import (
    enqueue_negotiation_proposal,
)


class NegotiationProposalTests(
    unittest.TestCase
):
    '''
    Pruebas unitarias para la función
    enqueue_negotiation_proposal().
    Prueba que la negociación se persiste
    correctamente y que el mensaje de
    negociación se encola en la tabla
    OutboundMessage con el estado
    PENDING y dispatch_required=True.
    '''

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

        self.cycle_id = (
            f"proposal-{uuid4()}"
        )

        now = datetime.now(
            timezone.utc
        )

        cycle = Cycle(
            cycle_id=self.cycle_id,

            generation_capacity=(
                Decimal("150.00")
            ),
            consumption=Decimal("100.00"),
            generation_cost=Decimal("210.00"),

            scheduler_state="NEGOTIATING",
            valid_until=(
                now
                + timedelta(minutes=10)
            ),

            opening_budget_balance=(
                Decimal("1000.00")
            ),
            opening_energy_balance=(
                Decimal("50.00")
            ),
            budget_balance=(
                Decimal("1000.00")
            ),
            energy_balance=(
                Decimal("50.00")
            ),

            last_sequence=0,
            status_payload={},
            created_at=now,
        )

        self.session.add(cycle)
        self.session.flush()

    def tearDown(self):
        self.session.close()
        self.transaction.rollback()
        self.connection.close()

    def test_valid_proposal_is_persisted_and_enqueued(
        self,
    ):
        negotiation = (
            enqueue_negotiation_proposal(
                self.session,
                cycle_id=self.cycle_id,
                direction="give",
                quantity=Decimal("40.00"),
                price_per_energy=(
                    Decimal("220.50")
                ),
                city_id="KLD",
                routing_key="central",
                commit=False,
            )
        )

        outbound = self.session.exec(
            select(OutboundMessage).where(
                OutboundMessage.msg_id
                == negotiation.latest_msg_id
            )
        ).one()

        self.assertEqual(
            negotiation.status,
            "PENDING_PUBLICATION",
        )

        self.assertEqual(
            outbound.status,
            "PENDING",
        )

        self.assertTrue(
            outbound.dispatch_required
        )

        self.assertEqual(
            outbound.message_type,
            "negotiation-proposal",
        )

        self.assertEqual(
            outbound.idpk,
            negotiation.idpk,
        )

        self.assertEqual(
            outbound.payload["cityId"],
            "KLD",
        )

        self.assertEqual(
            outbound.payload["cycleId"],
            self.cycle_id,
        )

        self.assertEqual(
            outbound.payload["data"][
                "direction"
            ],
            "give",
        )

        self.assertEqual(
            outbound.payload["data"][
                "quantity"
            ],
            40,
        )


if __name__ == "__main__":
    unittest.main()