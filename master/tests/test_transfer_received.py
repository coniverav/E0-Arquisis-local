import unittest
from decimal import Decimal
from datetime import datetime, timezone
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlmodel import Session, select

from app.database import engine, run_migrations
from app.main import app
from app.models import (
    Cycle,
    InboundMessage,
    LedgerEntry,
    ProcessedIdpk,
)


class TransferReceivedTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        run_migrations()
        cls.client = TestClient(app)

    def setUp(self):
        self.cycle_id = f"transfer-test-{uuid4()}"
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

    def _payload(
        self,
        *,
        msg_id: str,
        quantity: float,
    ):
        return {
            "idpk": self.idpk,
            "msgId": msg_id,
            "type": "transfer",
            "timestamp": datetime.now(
                timezone.utc
            ).isoformat(),
            "cycleId": self.cycle_id,
            "sender": "central",
            "data": {
                "quantity": quantity,
            },
        }

    def test_received_transfer_changes_budget_once_and_is_audited(self):
        first_payload = self._payload(
            msg_id=self.first_msg_id,
            quantity=120.50,
        )

        # Primera recepción del transfer.
        first_response = self.client.post(
            "/internal/messages",
            json=first_payload,
        )

        self.assertEqual(
            first_response.status_code,
            200,
        )

        self.assertEqual(
            first_response.json()["status"],
            "ok",
        )

        # ---------------------------------------------------------
        # El transfer debe afectar el budget exactamente una vez.
        # ---------------------------------------------------------
        with Session(engine) as session:
            cycle = session.get(
                Cycle,
                self.cycle_id,
            )

            self.assertIsNotNone(cycle)

            self.assertEqual(
                cycle.budget_balance,
                Decimal("120.50"),
            )

            self.assertEqual(
                cycle.energy_balance,
                Decimal("0.00"),
            )

            self.assertEqual(
                cycle.last_sequence,
                1,
            )

            self.assertEqual(
                cycle.last_operation_type,
                "TRANSFER_IN",
            )

            entries = session.exec(
                select(LedgerEntry)
                .where(
                    LedgerEntry.cycle_id
                    == self.cycle_id
                )
                .order_by(
                    LedgerEntry.sequence_no
                )
            ).all()

            self.assertEqual(
                len(entries),
                1,
            )

            entry = entries[0]

            self.assertEqual(
                entry.operation_type,
                "TRANSFER_IN",
            )

            self.assertEqual(
                entry.idpk,
                self.idpk,
            )

            self.assertEqual(
                entry.source_msg_id,
                self.first_msg_id,
            )

            self.assertEqual(
                entry.budget_delta,
                Decimal("120.50"),
            )

            self.assertEqual(
                entry.energy_delta,
                Decimal("0.00"),
            )

            self.assertEqual(
                entry.budget_after,
                Decimal("120.50"),
            )

        # ---------------------------------------------------------
        # La operación debe quedar visible en el historial.
        # ---------------------------------------------------------
        history_response = self.client.get(
            f"/cycles/{self.cycle_id}"
        )

        self.assertEqual(
            history_response.status_code,
            200,
        )

        history = history_response.json()

        self.assertEqual(
            Decimal(
                str(
                    history[
                        "finalBudgetBalance"
                    ]
                )
            ),
            Decimal("120.50"),
        )

        self.assertEqual(
            len(history["fundsReceived"]),
            1,
        )

        self.assertEqual(
            len(history["ledger"]),
            1,
        )

        self.assertEqual(
            history["fundsReceived"][0][
                "operationType"
            ],
            "TRANSFER_IN",
        )

        self.assertEqual(
            history["lastOperation"][
                "operationType"
            ],
            "TRANSFER_IN",
        )

        self.assertTrue(
            history["snapshotConsistent"]
        )

        # ---------------------------------------------------------
        # Retry de la misma operación.
        #
        # Debe conservar el mismo idpk pero usar un msgId nuevo.
        # Se cambia deliberadamente quantity para demostrar que
        # el efecto original no vuelve a aplicarse.
        # ---------------------------------------------------------
        retry_payload = self._payload(
            msg_id=self.retry_msg_id,
            quantity=999.99,
        )

        retry_response = self.client.post(
            "/internal/messages",
            json=retry_payload,
        )

        self.assertEqual(
            retry_response.status_code,
            200,
        )

        # ---------------------------------------------------------
        # El budget y el historial NO deben cambiar.
        # ---------------------------------------------------------
        with Session(engine) as session:
            cycle = session.get(
                Cycle,
                self.cycle_id,
            )

            self.assertEqual(
                cycle.budget_balance,
                Decimal("120.50"),
            )

            self.assertEqual(
                cycle.last_sequence,
                1,
            )

            entries = session.exec(
                select(LedgerEntry).where(
                    LedgerEntry.cycle_id
                    == self.cycle_id
                )
            ).all()

            self.assertEqual(
                len(entries),
                1,
            )

            # La evidencia del ledger sigue apuntando
            # al mensaje que realmente aplicó el efecto.
            self.assertEqual(
                entries[0].source_msg_id,
                self.first_msg_id,
            )

            inbound = session.exec(
                select(InboundMessage)
                .where(
                    InboundMessage.cycle_id
                    == self.cycle_id
                )
                .order_by(InboundMessage.id)
            ).all()

            self.assertEqual(
                len(inbound),
                2,
            )

            self.assertEqual(
                inbound[0].status,
                "PROCESSED",
            )

            self.assertEqual(
                inbound[1].status,
                "DUPLICATE",
            )

        # El historial consultable también debe conservar
        # un único efecto.
        final_history_response = self.client.get(
            f"/cycles/{self.cycle_id}"
        )

        final_history = (
            final_history_response.json()
        )

        self.assertEqual(
            len(final_history["fundsReceived"]),
            1,
        )

        self.assertEqual(
            len(final_history["ledger"]),
            1,
        )

        self.assertEqual(
            Decimal(
                str(
                    final_history[
                        "finalBudgetBalance"
                    ]
                )
            ),
            Decimal("120.50"),
        )

        self.assertTrue(
            final_history["snapshotConsistent"]
        )


if __name__ == "__main__":
    unittest.main()