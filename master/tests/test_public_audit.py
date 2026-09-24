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


class PublicAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        run_migrations()
        cls.client = TestClient(app)

    def setUp(self):
        self.created_ids = []

        self.duplicate_cycle_id = (
            f"public-audit-duplicate-{uuid4()}"
        )
        self.duplicate_idpk = str(uuid4())

    def tearDown(self):
        with Session(engine) as session:
            if self.created_ids:
                session.exec(
                    delete(InboundMessage).where(
                        InboundMessage.id.in_(self.created_ids)
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

    def test_discarded_anomaly_is_publicly_queryable_without_raw_payload(self):
        response = self.client.post(
            "/internal/audit/inbound",
            json={
                "status": "DISCARDED",
                "rawPayload": '{"msgId": "incompleto"',
                "reason": "El mensaje no es JSON válido",
            },
        )

        self.assertEqual(response.status_code, 200)

        created = response.json()
        self.created_ids.append(created["id"])

        query = self.client.get(
            "/audit/anomalies",
            params={
                "status": "DISCARDED",
            },
        )

        self.assertEqual(query.status_code, 200)

        item = next(
            anomaly
            for anomaly in query.json()["items"]
            if anomaly["id"] == created["id"]
        )

        self.assertEqual(item["status"], "DISCARDED")
        self.assertEqual(
            item["reason"],
            "El mensaje no es JSON válido",
        )

        self.assertIn("receivedAt", item)
        self.assertIn("relatedMsgId", item)

        self.assertNotIn("payload", item)
        self.assertNotIn("rawPayload", item)

    def test_nacked_anomaly_exposes_rf05_evidence(self):
        msg_id = str(uuid4())
        idpk = str(uuid4())

        response = self.client.post(
            "/internal/audit/inbound",
            json={
                "status": "NACKED",
                "msgId": msg_id,
                "idpk": idpk,
                "type": "status-statement",
                "cycleId": f"public-audit-{uuid4()}",
                "sender": "central",
                "payload": {
                    "msgId": msg_id,
                    "idpk": idpk,
                    "type": "status-statement",
                },
                "reasonCode": "MALFORMED_MESSAGE",
                "reason": "Falta data.energy.consumption",
            },
        )

        self.assertEqual(response.status_code, 200)

        created = response.json()
        self.created_ids.append(created["id"])

        query = self.client.get(
            "/audit/anomalies",
            params={
                "status": "NACKED",
                "reasonCode": "MALFORMED_MESSAGE",
                "msgId": msg_id,
            },
        )

        self.assertEqual(query.status_code, 200)

        body = query.json()

        self.assertEqual(body["total"], 1)
        self.assertEqual(len(body["items"]), 1)

        item = body["items"][0]

        self.assertEqual(item["type"], "status-statement")
        self.assertEqual(item["status"], "NACKED")
        self.assertEqual(
            item["reasonCode"],
            "MALFORMED_MESSAGE",
        )
        self.assertEqual(
            item["reason"],
            "Falta data.energy.consumption",
        )
        self.assertEqual(item["msgId"], msg_id)
        self.assertEqual(item["idpk"], idpk)
        self.assertEqual(item["relatedMsgId"], msg_id)

        self.assertIn("receivedAt", item)
        self.assertNotIn("payload", item)
        self.assertNotIn("rawPayload", item)

    def test_duplicate_anomaly_links_retry_to_original_message(self):
        now = datetime.now(timezone.utc)

        original_msg_id = str(uuid4())
        retry_msg_id = str(uuid4())

        original_payload = {
            "idpk": self.duplicate_idpk,
            "msgId": original_msg_id,
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

        retry_payload = {
            **original_payload,
            "msgId": retry_msg_id,
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
            "/audit/anomalies",
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

        self.assertEqual(body["total"], 1)
        self.assertEqual(len(body["items"]), 1)

        duplicate = body["items"][0]

        self.assertEqual(
            duplicate["msgId"],
            retry_msg_id,
        )
        self.assertEqual(
            duplicate["relatedMsgId"],
            original_msg_id,
        )
        self.assertEqual(
            duplicate["status"],
            "DUPLICATE",
        )
        self.assertEqual(
            duplicate["type"],
            "status-statement",
        )
        self.assertEqual(
            duplicate["reason"],
            "idpk already processed",
        )

        self.assertIn(
            "receivedAt",
            duplicate,
        )

        self.assertNotIn(
            "payload",
            duplicate,
        )
        self.assertNotIn(
            "rawPayload",
            duplicate,
        )