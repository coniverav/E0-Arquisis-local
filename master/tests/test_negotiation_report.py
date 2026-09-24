import unittest
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from sqlmodel import Session, select, SQLModel

from app.database import engine

from app.models import (
    Cycle,
    NegotiationReport,
)
from app.services.ledger import apply_ledger_effect
from app.services.negotiation_report import (
    generate_negotiation_report,
)


class NegotiationReportTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # Crea las tablas registradas en SQLModel si todavía no existen.
        # No elimina ni modifica datos existentes.
        SQLModel.metadata.create_all(engine)

    def setUp(self):

        self.connection = engine.connect()
        self.transaction = self.connection.begin()

        self.session = Session(
            bind=self.connection
        )

        self.cycle_id = (
            f"cycle-test-{uuid4()}"
        )

        cycle = Cycle(
            cycle_id=self.cycle_id,

            opening_budget_balance=Decimal(
                "1000.00"
            ),
            opening_energy_balance=Decimal(
                "-30.00"
            ),

            budget_balance=Decimal(
                "1000.00"
            ),
            energy_balance=Decimal(
                "-30.00"
            ),

            last_sequence=0,

            status_payload={},

            created_at=datetime.now(
                timezone.utc
            ),
        )

        self.session.add(cycle)
        self.session.flush()

    def tearDown(self):

        self.session.close()
        self.transaction.rollback()
        self.connection.close()

    # -------------------------------------------------
    # Report refleja exactamente el ledger
    # -------------------------------------------------

    def test_report_matches_ledger(self):

        apply_ledger_effect(
            self.session,
            cycle_id=self.cycle_id,
            idpk=str(uuid4()),
            source_msg_id=str(uuid4()),
            operation_type="TRANSFER_IN",
            budget_delta=Decimal("500.00"),
            energy_delta=Decimal("0.00"),
            details={},
        )

        apply_ledger_effect(
            self.session,
            cycle_id=self.cycle_id,
            idpk=str(uuid4()),
            source_msg_id=str(uuid4()),
            operation_type="DEMAND_STATEMENT",
            budget_delta=Decimal("-50.00"),
            energy_delta=Decimal("10.00"),
            details={},
        )

        report = generate_negotiation_report(
            self.session,
            cycle_id=self.cycle_id,
            city_id="TEST",
        )

        self.assertEqual(
            report.budget_balance,
            Decimal("1450.00"),
        )

        self.assertEqual(
            report.energy_balance,
            Decimal("-20.00"),
        )

        self.assertEqual(
            report.payload["data"][
                "budgetBalance"
            ],
            1450,
        )

        self.assertEqual(
            report.payload["data"][
                "energyBalance"
            ],
            -20,
        )

        # E1-40 genera pero NO envía.
        self.assertIsNone(
            report.sent_at
        )

    # -------------------------------------------------
    # Un solo report por ciclo
    # -------------------------------------------------

    def test_report_is_generated_only_once(self):

        first = generate_negotiation_report(
            self.session,
            cycle_id=self.cycle_id,
            city_id="TEST",
        )

        second = generate_negotiation_report(
            self.session,
            cycle_id=self.cycle_id,
            city_id="TEST",
        )

        self.assertEqual(
            first.idpk,
            second.idpk,
        )

        self.assertEqual(
            first.msg_id,
            second.msg_id,
        )

        reports = self.session.exec(
            select(NegotiationReport).where(
                NegotiationReport.cycle_id
                == self.cycle_id
            )
        ).all()

        self.assertEqual(
            len(reports),
            1,
        )

    # -------------------------------------------------
    # Snapshot inconsistente
    # -------------------------------------------------

    def test_inconsistent_ledger_is_rejected(self):

        cycle = self.session.get(
            Cycle,
            self.cycle_id,
        )

        # Simulamos corrupción del snapshot.
        cycle.budget_balance = Decimal(
            "999999.00"
        )

        self.session.add(cycle)
        self.session.flush()

        with self.assertRaises(ValueError):

            generate_negotiation_report(
                self.session,
                cycle_id=self.cycle_id,
                city_id="TEST",
            )


if __name__ == "__main__":
    unittest.main()