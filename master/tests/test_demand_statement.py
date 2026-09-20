import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlmodel import Session, select

from app.database import engine, run_migrations
from app.main import app
from app.models import Cycle, InboundMessage, LedgerEntry, ProcessedIdpk


class DemandStatementTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        run_migrations()
        cls.client = TestClient(app)

    def setUp(self):
        self.cycle_id = f"demand-test-{uuid4()}"

    def tearDown(self):
        with Session(engine) as session:
            session.exec(
                delete(InboundMessage).where(
                    InboundMessage.cycle_id == self.cycle_id
                )
            )

            session.exec(
                delete(LedgerEntry).where(
                    LedgerEntry.cycle_id == self.cycle_id
                )
            )

            session.exec(
                delete(ProcessedIdpk).where(
                    ProcessedIdpk.cycle_id == self.cycle_id
                )
            )

            cycle = session.get(Cycle, self.cycle_id)

            if cycle is not None:
                session.delete(cycle)

            session.commit()

    def _apply_status_statement(self):
        now = datetime.now(timezone.utc)

        payload = {
            "idpk": str(uuid4()),
            "msgId": str(uuid4()),
            "type": "status-statement",
            "timestamp": now.isoformat(),
            "cycleId": self.cycle_id,
            "sender": "central",
            "data": {
                "energy": {
                    "generationCapacity": 100,
                    "consumption": 80,
                    "generationCost": 5,
                },
                "validUntil": (
                    now + timedelta(hours=1)
                ).isoformat(),
            },
        }

        response = self.client.post(
            "/internal/messages",
            json=payload,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def _demand_payload(
        self,
        *,
        quantity: float,
        value_per_kwh: float,
    ):
        return {
            "idpk": str(uuid4()),
            "msgId": str(uuid4()),
            "type": "demand-statement",
            "timestamp": datetime.now(
                timezone.utc
            ).isoformat(),
            "cycleId": self.cycle_id,
            "sender": "central",
            "data": {
                "balance": {
                    "quantity": quantity,
                    "valuePerKwh": value_per_kwh,
                }
            },
        }

    def test_positive_quantity_increases_energy_and_decreases_budget(self):
        self._apply_status_statement()

        payload = self._demand_payload(
            quantity=10,
            value_per_kwh=5,
        )

        response = self.client.post(
            "/internal/messages",
            json=payload,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

        with Session(engine) as session:
            cycle = session.get(Cycle, self.cycle_id)

            self.assertIsNotNone(cycle)

            self.assertEqual(
                cycle.energy_balance,
                Decimal("30.00"),
            )

            self.assertEqual(
                cycle.budget_balance,
                Decimal("-50.00"),
            )

            entries = session.exec(
                select(LedgerEntry).where(
                    LedgerEntry.cycle_id == self.cycle_id
                )
            ).all()

            self.assertEqual(len(entries), 1)

            entry = entries[0]

            self.assertEqual(
                entry.operation_type,
                "DEMAND_STATEMENT",
            )

            self.assertEqual(
                entry.energy_delta,
                Decimal("10.00"),
            )

            self.assertEqual(
                entry.budget_delta,
                Decimal("-50.00"),
            )

            self.assertEqual(
                entry.energy_after,
                Decimal("30.00"),
            )

            self.assertEqual(
                entry.budget_after,
                Decimal("-50.00"),
            )

        history_response = self.client.get(
            f"/cycles/{self.cycle_id}"
        )

        self.assertEqual(
            history_response.status_code,
            200,
        )

        history = history_response.json()

        self.assertEqual(
            len(history["demandStatements"]),
            1,
        )

        self.assertEqual(
            Decimal(str(history["finalEnergyBalance"])),
            Decimal("30.00"),
        )

        self.assertEqual(
            Decimal(str(history["finalBudgetBalance"])),
            Decimal("-50.00"),
        )

        self.assertEqual(
            history["lastOperation"]["operationType"],
            "DEMAND_STATEMENT",
        )

        self.assertTrue(history["snapshotConsistent"])

    def test_negative_quantity_decreases_energy_and_increases_budget(self):
        self._apply_status_statement()

        payload = self._demand_payload(
            quantity=-4,
            value_per_kwh=5,
        )

        response = self.client.post(
            "/internal/messages",
            json=payload,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

        with Session(engine) as session:
            cycle = session.get(Cycle, self.cycle_id)

            self.assertIsNotNone(cycle)

            self.assertEqual(
                cycle.energy_balance,
                Decimal("16.00"),
            )

            self.assertEqual(
                cycle.budget_balance,
                Decimal("20.00"),
            )

            entries = session.exec(
                select(LedgerEntry).where(
                    LedgerEntry.cycle_id == self.cycle_id
                )
            ).all()

            self.assertEqual(len(entries), 1)

            entry = entries[0]

            self.assertEqual(
                entry.operation_type,
                "DEMAND_STATEMENT",
            )

            self.assertEqual(
                entry.energy_delta,
                Decimal("-4.00"),
            )

            self.assertEqual(
                entry.budget_delta,
                Decimal("20.00"),
            )

            self.assertEqual(
                entry.energy_after,
                Decimal("16.00"),
            )

            self.assertEqual(
                entry.budget_after,
                Decimal("20.00"),
            )

        history_response = self.client.get(
            f"/cycles/{self.cycle_id}"
        )

        self.assertEqual(
            history_response.status_code,
            200,
        )

        history = history_response.json()

        self.assertEqual(
            len(history["demandStatements"]),
            1,
        )

        self.assertEqual(
            Decimal(str(history["finalEnergyBalance"])),
            Decimal("16.00"),
        )

        self.assertEqual(
            Decimal(str(history["finalBudgetBalance"])),
            Decimal("20.00"),
        )

        self.assertEqual(
            history["lastOperation"]["operationType"],
            "DEMAND_STATEMENT",
        )

        self.assertTrue(history["snapshotConsistent"])


if __name__ == "__main__":
    unittest.main()