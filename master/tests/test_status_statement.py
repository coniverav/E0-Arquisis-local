import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlmodel import Session

from app.database import engine, run_migrations
from app.main import app
from app.models import Cycle, InboundMessage, ProcessedIdpk


class StatusStatementTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        run_migrations()
        cls.client = TestClient(app)

    def setUp(self):
        self.cycle_id = f"status-test-{uuid4()}"
        self.idpk = str(uuid4())
        self.first_msg_id = str(uuid4())
        self.retry_msg_id = str(uuid4())

    def tearDown(self):
        with Session(engine) as session:
            session.exec(
                delete(InboundMessage).where(
                    InboundMessage.cycle_id == self.cycle_id
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

    def _payload(
        self,
        *,
        msg_id: str,
        generation: int = 150,
        consumption: int = 100,
        generation_cost: int = 20,
    ):
        now = datetime.now(timezone.utc)

        return {
            "idpk": self.idpk,
            "msgId": msg_id,
            "type": "status-statement",
            "timestamp": now.isoformat(),
            "cycleId": self.cycle_id,
            "sender": "central",
            "data": {
                "energy": {
                    "generationCapacity": generation,
                    "consumption": consumption,
                    "generationCost": generation_cost,
                },
                "validUntil": (
                    now + timedelta(hours=1)
                ).isoformat(),
            },
        }

    def test_status_statement_is_applied_once_and_visible_in_history(self):
        first_payload = self._payload(
            msg_id=self.first_msg_id,
        )

        first_response = self.client.post(
            "/internal/messages",
            json=first_payload,
        )

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(
            first_response.json()["status"],
            "ok",
        )

        # Verifica el estado persistido después de la primera aplicación.
        with Session(engine) as session:
            cycle = session.get(Cycle, self.cycle_id)

            self.assertIsNotNone(cycle)

            self.assertEqual(
                cycle.status_idpk,
                self.idpk,
            )
            self.assertEqual(
                cycle.status_msg_id,
                self.first_msg_id,
            )

            self.assertEqual(
                cycle.generation_capacity,
                Decimal("150.00"),
            )
            self.assertEqual(
                cycle.consumption,
                Decimal("100.00"),
            )
            self.assertEqual(
                cycle.generation_cost,
                Decimal("20.00"),
            )

            self.assertEqual(
                cycle.opening_energy_balance,
                Decimal("50.00"),
            )
            self.assertEqual(
                cycle.energy_balance,
                Decimal("50.00"),
            )

        # Debe quedar disponible en el historial del ciclo.
        history_response = self.client.get(
            f"/cycles/{self.cycle_id}"
        )

        self.assertEqual(
            history_response.status_code,
            200,
        )

        history = history_response.json()

        self.assertEqual(
            history["cycleId"],
            self.cycle_id,
        )

        self.assertEqual(
            history["statusStatement"],
            first_payload["data"],
        )

        self.assertTrue(
            history["snapshotConsistent"]
        )

        # Retry de la MISMA operación:
        # conserva idpk pero utiliza un msgId nuevo.
        #
        # Se cambian deliberadamente los valores para demostrar
        # que el status no se vuelve a aplicar.
        retry_payload = self._payload(
            msg_id=self.retry_msg_id,
            generation=999,
            consumption=1,
            generation_cost=999,
        )

        retry_response = self.client.post(
            "/internal/messages",
            json=retry_payload,
        )

        self.assertEqual(
            retry_response.status_code,
            200,
        )

        self.assertEqual(
            retry_response.json()["status"],
            "duplicate",
        )

        # La segunda recepción no puede modificar el ciclo.
        with Session(engine) as session:
            cycle = session.get(Cycle, self.cycle_id)

            self.assertEqual(
                cycle.status_idpk,
                self.idpk,
            )

            # Se conserva el msgId que realmente aplicó el status.
            self.assertEqual(
                cycle.status_msg_id,
                self.first_msg_id,
            )

            self.assertEqual(
                cycle.generation_capacity,
                Decimal("150.00"),
            )
            self.assertEqual(
                cycle.consumption,
                Decimal("100.00"),
            )

            self.assertEqual(
                cycle.opening_energy_balance,
                Decimal("50.00"),
            )
            self.assertEqual(
                cycle.energy_balance,
                Decimal("50.00"),
            )

        # El historial también debe permanecer sin modificaciones.
        final_history_response = self.client.get(
            f"/cycles/{self.cycle_id}"
        )

        final_history = final_history_response.json()

        self.assertEqual(
            final_history["statusStatement"],
            first_payload["data"],
        )

        self.assertTrue(
            final_history["snapshotConsistent"]
        )


if __name__ == "__main__":
    unittest.main()