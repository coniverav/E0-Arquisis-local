import unittest
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlmodel import Session

from app.database import engine, run_migrations
from app.main import app
from app.models import (
    Cycle,
    InboundMessage,
    ProcessedIdpk,
)

class InboundAuditQueryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        run_migrations()
        cls.client = TestClient(app)

    def setUp(self):
        self.created_audit_ids = []

        self.nack_msg_id = str(uuid4())
        self.nack_idpk = str(uuid4())
        self.nack_cycle_id = f"audit-nack-{uuid4()}"

        self.duplicate_cycle_id = (
            f"audit-duplicate-{uuid4()}"
        )
        self.duplicate_idpk = str(uuid4())
        self.original_msg_id = str(uuid4())
        self.retry_msg_id = str(uuid4())

    def tearDown(self):
        with Session(engine) as session:
            if self.created_audit_ids:
                session.exec(
                    delete(InboundMessage).where(
                        InboundMessage.id.in_(
                            self.created_audit_ids
                        )
                    )
                )

            session.exec(
                delete(InboundMessage).where(
                    InboundMessage.cycle_id
                    == self.duplicate_cycle_id
                )
            )

            session.exec(
                delete(ProcessedIdpk).where(
                    ProcessedIdpk.idpk
                    == self.duplicate_idpk
                )
            )

            cycle = session.get(
                Cycle,
                self.duplicate_cycle_id,
            )

            if cycle is not None:
                session.delete(cycle)

            session.commit()

    #Un mensaje descartado puede no tener msgId ni idpk. Se conserva igualmente la evidencia cruda y la razón.
    def test_discarded_message_is_queryable(self):
        response = self.client.post(
            "/internal/audit/inbound",
            json={
                "status": "DISCARDED",
                "rawPayload": '{"msgId": "incompleto"',
                "reason": "El mensaje no es JSON válido",
            },
        )

        self.assertEqual(
            response.status_code,
            200,
        )

        created = response.json()

        self.created_audit_ids.append(
            created["id"]
        )

        self.assertEqual(
            created["status"],
            "DISCARDED",
        )
        self.assertIsNone(
            created["msgId"]
        )
        self.assertIsNone(
            created["idpk"]
        )
        self.assertEqual(
            created["reason"],
            "El mensaje no es JSON válido",
        )
        self.assertEqual(
            created["rawPayload"],
            '{"msgId": "incompleto"',
        )

        query_response = self.client.get(
            "/internal/audit/inbound",
            params={
                "status": "DISCARDED",
            },
        )

        self.assertEqual(
            query_response.status_code,
            200,
        )

        body = query_response.json()

        stored = [
            item
            for item in body["items"]
            if item["id"] == created["id"]
        ]

        self.assertEqual(
            len(stored),
            1,
        )
        self.assertEqual(
            stored[0]["status"],
            "DISCARDED",
        )
        self.assertEqual(
            stored[0]["reason"],
            "El mensaje no es JSON válido",
        )

    def test_nacked_message_keeps_reason_and_is_queryable(self):
        original_payload = {
            "idpk": self.nack_idpk,
            "msgId": self.nack_msg_id,
            "type": "status-statement",
            "timestamp": datetime.now(
                timezone.utc
            ).isoformat(),
            "sender": "central",
            "cycleId": self.nack_cycle_id,
            "data": {
                "energy": {
                    "generationCapacity": 100,
                }
            },
        }

        response = self.client.post(
            "/internal/audit/inbound",
            json={
                "status": "NACKED",
                "msgId": self.nack_msg_id,
                "idpk": self.nack_idpk,
                "type": "status-statement",
                "cycleId": self.nack_cycle_id,
                "sender": "central",
                "payload": original_payload,
                "reasonCode": "MALFORMED_MESSAGE",
                "reason": (
                    "Falta data.energy.consumption"
                ),
            },
        )

        self.assertEqual(
            response.status_code,
            200,
        )

        created = response.json()

        self.created_audit_ids.append(
            created["id"]
        )

        self.assertEqual(
            created["status"],
            "NACKED",
        )
        self.assertEqual(
            created["reasonCode"],
            "MALFORMED_MESSAGE",
        )
        self.assertEqual(
            created["msgId"],
            self.nack_msg_id,
        )

        query_response = self.client.get(
            "/internal/audit/inbound",
            params={
                "status": "NACKED",
                "reasonCode": (
                    "MALFORMED_MESSAGE"
                ),
                "msgId": self.nack_msg_id,
            },
        )

        self.assertEqual(
            query_response.status_code,
            200,
        )

        body = query_response.json()

        self.assertEqual(
            body["total"],
            1,
        )
        self.assertEqual(
            len(body["items"]),
            1,
        )

        item = body["items"][0]

        self.assertEqual(
            item["status"],
            "NACKED",
        )
        self.assertEqual(
            item["reasonCode"],
            "MALFORMED_MESSAGE",
        )
        self.assertEqual(
            item["reason"],
            "Falta data.energy.consumption",
        )
        self.assertEqual(
            item["msgId"],
            self.nack_msg_id,
        )
        self.assertEqual(
            item["idpk"],
            self.nack_idpk,
        )

    def test_duplicate_links_retry_to_original_message(self):
        now = datetime.now(timezone.utc)

        original_payload = {
            "idpk": self.duplicate_idpk,
            "msgId": self.original_msg_id,
            "type": "status-statement",
            "timestamp": now.isoformat(),
            "cycleId": self.duplicate_cycle_id,
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

        first_response = self.client.post(
            "/internal/messages",
            json=original_payload,
        )

        self.assertEqual(
            first_response.status_code,
            200,
        )
        self.assertEqual(
            first_response.json()["status"],
            "ok",
        )

        #El retry conserva idpk pero utiliza otro msgId.
        retry_payload = {
            **original_payload,
            "msgId": self.retry_msg_id,
        }

        second_response = self.client.post(
            "/internal/messages",
            json=retry_payload,
        )

        self.assertEqual(
            second_response.status_code,
            200,
        )
        self.assertEqual(
            second_response.json()["status"],
            "duplicate",
        )

        query_response = self.client.get(
            "/internal/audit/inbound",
            params={
                "status": "DUPLICATE",
                "idpk": self.duplicate_idpk,
            },
        )

        self.assertEqual(
            query_response.status_code,
            200,
        )

        body = query_response.json()

        self.assertEqual(
            body["total"],
            1,
        )
        self.assertEqual(
            len(body["items"]),
            1,
        )

        duplicate = body["items"][0]

        #msgId identifica la recepción duplicada.
        self.assertEqual(
            duplicate["msgId"],
            self.retry_msg_id,
        )

        #relatedMsgId identifica el mensaje que obtuvo originalmente el claim del idpk.
        self.assertEqual(
            duplicate["relatedMsgId"],
            self.original_msg_id,
        )

        self.assertEqual(
            duplicate["status"],
            "DUPLICATE",
        )
        self.assertEqual(
            duplicate["reason"],
            "idpk already processed",
        )


if __name__ == "__main__":
    unittest.main()