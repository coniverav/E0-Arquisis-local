import unittest
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlmodel import Session, select

from app.database import engine, run_migrations
from app.main import app
from app.models import OutboundMessage


class OutboundMessageAuditTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        run_migrations()
        cls.client = TestClient(app)

    def setUp(self):
        self.msg_id = str(uuid4())
        self.idpk = str(uuid4())

    def tearDown(self):
        with Session(engine) as session:
            session.exec(
                delete(OutboundMessage).where(
                    OutboundMessage.msg_id == self.msg_id
                )
            )
            session.commit()

    def test_outbound_message_is_audited_and_published_idempotently(self):
        create_response = self.client.post(
            "/internal/audit/outbound",
            json={
                "msgId": self.msg_id,
                "idpk": self.idpk,
                "type": "ack",
                "routingKey": "central.test",
                "payload": {
                    "type": "ack",
                },
            },
        )

        self.assertEqual(create_response.status_code, 200)
        self.assertEqual(
            create_response.json()["status"],
            "PENDING",
        )
        self.assertFalse(
            create_response.json()["duplicate"]
        )

        first_result = self.client.post(
            f"/internal/audit/outbound/{self.msg_id}/result",
            json={
                "status": "PUBLISHED",
            },
        )

        self.assertEqual(first_result.status_code, 200)
        self.assertEqual(
            first_result.json()["status"],
            "PUBLISHED",
        )
        self.assertEqual(
            first_result.json()["attemptCount"],
            1,
        )

        # Repetir el mismo resultado no debe modificar
        # nuevamente la evidencia.
        second_result = self.client.post(
            f"/internal/audit/outbound/{self.msg_id}/result",
            json={
                "status": "PUBLISHED",
            },
        )

        self.assertEqual(second_result.status_code, 200)
        self.assertEqual(
            second_result.json()["attemptCount"],
            1,
        )

        with Session(engine) as session:
            stored = session.exec(
                select(OutboundMessage).where(
                    OutboundMessage.msg_id == self.msg_id
                )
            ).one()

            self.assertEqual(stored.status, "PUBLISHED")
            self.assertEqual(stored.attempt_count, 1)
            self.assertIsNotNone(stored.published_at)


if __name__ == "__main__":
    unittest.main()
