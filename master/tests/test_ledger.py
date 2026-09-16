import unittest
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from sqlmodel import Session

from app.database import create_db_and_tables, engine
from app.models import Cycle
from app.services.ledger import (
    apply_ledger_effect,
    rebuild_cycle_balances,
)


class LedgerServiceTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # Garantiza que las tablas E1 existan.
        create_db_and_tables()

    def setUp(self):
        """
        Cada test corre dentro de una transacción real de PostgreSQL.
        Al terminar hacemos rollback para no contaminar la DB.
        """
        self.connection = engine.connect()
        self.transaction = self.connection.begin()
        self.session = Session(bind=self.connection)

        self.cycle_id = f"cycle-test-{uuid4()}"

        cycle = Cycle(
            cycle_id=self.cycle_id,

            opening_budget_balance=Decimal("1000.00"),
            opening_energy_balance=Decimal("-30.00"),

            budget_balance=Decimal("1000.00"),
            energy_balance=Decimal("-30.00"),

            last_sequence=0,
            status_payload={},
            created_at=datetime.now(timezone.utc),
        )

        self.session.add(cycle)
        self.session.flush()

    def tearDown(self):
        self.session.close()
        self.transaction.rollback()
        self.connection.close()

    def test_rebuild_matches_snapshot(self):

        # La central entrega fondos.
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

        # Demand statement:
        # +10 energía y -50 presupuesto.
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

        rebuilt_budget, rebuilt_energy = rebuild_cycle_balances(
            self.session,
            self.cycle_id,
        )

        cycle = self.session.get(Cycle, self.cycle_id)

        self.assertEqual(cycle.budget_balance, Decimal("1450.00"))
        self.assertEqual(cycle.energy_balance, Decimal("-20.00"))

        # Lo reconstruido debe coincidir exactamente con el snapshot.
        self.assertEqual(rebuilt_budget, cycle.budget_balance)
        self.assertEqual(rebuilt_energy, cycle.energy_balance)

    def test_duplicate_is_not_applied_twice(self):

        operation_idpk = str(uuid4())

        _, applied_first = apply_ledger_effect(
            self.session,
            cycle_id=self.cycle_id,
            idpk=operation_idpk,
            source_msg_id=str(uuid4()),
            operation_type="TRANSFER_IN",
            budget_delta=Decimal("500.00"),
            energy_delta=Decimal("0.00"),
            details={},
        )

        _, applied_second = apply_ledger_effect(
            self.session,
            cycle_id=self.cycle_id,
            idpk=operation_idpk,
            source_msg_id=str(uuid4()),
            operation_type="TRANSFER_IN",
            budget_delta=Decimal("500.00"),
            energy_delta=Decimal("0.00"),
            details={},
        )

        cycle = self.session.get(Cycle, self.cycle_id)

        self.assertTrue(applied_first)
        self.assertFalse(applied_second)

        # 1000 + 500, NO 1000 + 500 + 500.
        self.assertEqual(cycle.budget_balance, Decimal("1500.00"))
        self.assertEqual(cycle.last_sequence, 1)


if __name__ == "__main__":
    unittest.main()