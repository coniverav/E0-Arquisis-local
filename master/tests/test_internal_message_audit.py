import unittest
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlmodel import Session, select

from app.database import engine, run_migrations
from app.main import app
from app.models import Cycle, InboundMessage, ProcessedIdpk


class InternalMessageAuditTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        run_migrations()
        cls.client = TestClient(app)

    def setUp(self):
        self.cycle_id = f"audit-test-{uuid4()}"
        self.idpk = str(uuid4())
        self.msg_id = str(uuid4())

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

    def _status_statement_payload(self):
        now = datetime.now(timezone.utc)

        return {
            "idpk": self.idpk,
            "msgId": self.msg_id,
            "type": "status-statement",
            "timestamp": now.isoformat(),
            "cycleId": self.cycle_id,
            "sender": "central",
            "data": {
                "energy": {
                    "generationCapacity": 150,
                    "consumption": 100,
                    "generationCost": 20,
                },
                "validUntil": (
                    now + timedelta(hours=1)
                ).isoformat(),
            },
        }

    def test_status_statement_and_redelivery_are_audited(self):
        payload = self._status_statement_payload()

        # Primera recepción.
        first_response = self.client.post(
            "/internal/messages",
            json=payload,
        )

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(
            first_response.json()["status"],
            "ok",
        )

        # Redelivery exacta del mismo mensaje.
        second_response = self.client.post(
            "/internal/messages",
            json=payload,
        )

        self.assertEqual(second_response.status_code, 200)
        self.assertEqual(
            second_response.json()["status"],
            "duplicate",
        )

        # Ambas recepciones deben quedar persistidas.
        with Session(engine) as session:
            messages = session.exec(
                select(InboundMessage)
                .where(
                    InboundMessage.cycle_id
                    == self.cycle_id
                )
                .order_by(InboundMessage.id)
            ).all()

            self.assertEqual(len(messages), 2)

            first = messages[0]
            duplicate = messages[1]

            self.assertEqual(
                first.status,
                "PROCESSED",
            )

            self.assertEqual(
                duplicate.status,
                "DUPLICATE",
            )

            # La redelivery conserva los identificadores
            # originales pero genera una nueva evidencia.
            self.assertNotEqual(
                first.id,
                duplicate.id,
            )

            self.assertEqual(
                first.msg_id,
                duplicate.msg_id,
            )

            self.assertEqual(
                first.idpk,
                duplicate.idpk,
            )

            self.assertEqual(
                duplicate.reason,
                "idpk already processed",
            )


if __name__ == "__main__":
    unittest.main()
    