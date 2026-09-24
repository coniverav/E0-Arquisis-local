import unittest
from datetime import datetime, timedelta, timezone
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
    OutboundMessage,
    ProcessedIdpk,
)
from app.services.information_requests import (
    enqueue_information_request,
    resolve_information_request,
)


class InformationRequestTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        run_migrations()
        cls.client = TestClient(app)

    def setUp(self):
        self.cycle_id = f"e139-test-{uuid4()}"
        self.response_idpk = str(uuid4())
        self.response_msg_id = str(uuid4())

        with Session(engine) as session:
            session.exec(
                delete(OutboundMessage).where(
                    OutboundMessage.message_type == "request"
                )
            )
            session.commit()

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

            cycle = session.get(
                Cycle,
                self.cycle_id,
            )

            if cycle is not None:
                session.delete(cycle)

            session.exec(
                delete(OutboundMessage).where(
                    OutboundMessage.message_type == "request"
                )
            )

            session.commit()

    def test_request_is_enqueued_and_deduplicated(self):
        with Session(engine) as session:
            first, first_created = enqueue_information_request(
                session,
                ask="status-statement",
                city_id="KLD",
                routing_key="central",
            )

            second, second_created = enqueue_information_request(
                session,
                ask="status-statement",
                city_id="KLD",
                routing_key="central",
            )

            self.assertTrue(first_created)
            self.assertFalse(second_created)

            self.assertEqual(
                first.id,
                second.id,
            )

            self.assertEqual(
                first.message_type,
                "request",
            )

            self.assertEqual(
                first.request_ask,
                "status-statement",
            )

            self.assertTrue(
                first.dispatch_required
            )

            self.assertIsNone(
                first.cycle_id
            )

            self.assertNotIn(
                "cycleId",
                first.payload,
            )

            self.assertEqual(
                first.payload["data"]["ask"],
                "status-statement",
            )

    def test_unsupported_request_is_rejected(self):
        with Session(engine) as session:
            with self.assertRaises(ValueError):
                enqueue_information_request(
                    session,
                    ask="unknown-message",
                    city_id="KLD",
                    routing_key="central",
                )

    def test_pending_request_cannot_be_resolved(self):
        with Session(engine) as session:
            request, _ = enqueue_information_request(
                session,
                ask="status-statement",
                city_id="KLD",
                routing_key="central",
            )

            resolved = resolve_information_request(
                session,
                response_type="status-statement",
                response_msg_id=self.response_msg_id,
                response_idpk=self.response_idpk,
            )

            self.assertIsNone(resolved)

            session.refresh(request)

            self.assertIsNone(
                request.resolved_at
            )

    def test_dispatch_endpoint_and_published_state(self):
        with Session(engine) as session:
            request, _ = enqueue_information_request(
                session,
                ask="status-statement",
                city_id="KLD",
                routing_key="central",
            )

            request_msg_id = request.msg_id

        response = self.client.get(
            "/internal/audit/outbound/dispatch"
        )

        self.assertEqual(
            response.status_code,
            200,
        )

        items = response.json()["items"]

        matching = [
            item
            for item in items
            if item["msgId"] == request_msg_id
        ]

        self.assertEqual(
            len(matching),
            1,
        )

        publish_response = self.client.post(
            (
                "/internal/audit/outbound/"
                f"{request_msg_id}/result"
            ),
            json={
                "status": "PUBLISHED",
            },
        )

        self.assertEqual(
            publish_response.status_code,
            200,
        )

        with Session(engine) as session:
            stored = session.exec(
                select(OutboundMessage).where(
                    OutboundMessage.msg_id
                    == request_msg_id
                )
            ).first()

            self.assertEqual(
                stored.status,
                "PUBLISHED",
            )

            self.assertFalse(
                stored.dispatch_required
            )

    def test_status_statement_resolves_published_request(self):
        with Session(engine) as session:
            request, _ = enqueue_information_request(
                session,
                ask="status-statement",
                city_id="KLD",
                routing_key="central",
            )

            request_msg_id = request.msg_id

        publish_response = self.client.post(
            (
                "/internal/audit/outbound/"
                f"{request_msg_id}/result"
            ),
            json={
                "status": "PUBLISHED",
            },
        )

        self.assertEqual(
            publish_response.status_code,
            200,
        )

        now = datetime.now(timezone.utc)

        status_payload = {
            "idpk": self.response_idpk,
            "msgId": self.response_msg_id,
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
                    now + timedelta(minutes=20)
                ).isoformat(),
            },
        }

        response = self.client.post(
            "/internal/messages",
            json=status_payload,
        )

        self.assertEqual(
            response.status_code,
            200,
        )

        self.assertEqual(
            response.json()["status"],
            "ok",
        )

        with Session(engine) as session:
            stored = session.exec(
                select(OutboundMessage).where(
                    OutboundMessage.msg_id
                    == request_msg_id
                )
            ).first()

            self.assertIsNotNone(
                stored.resolved_at
            )

            self.assertEqual(
                stored.response_msg_id,
                self.response_msg_id,
            )

            self.assertEqual(
                stored.response_idpk,
                self.response_idpk,
            )

    def test_missing_status_is_requested_automatically(self):
        now = datetime.now(timezone.utc)

        transfer_payload = {
            "idpk": self.response_idpk,
            "msgId": self.response_msg_id,
            "type": "transfer",
            "timestamp": now.isoformat(),
            "cycleId": self.cycle_id,
            "sender": "central",
            "data": {
                "quantity": 100,
            },
        }

        response = self.client.post(
            "/internal/messages",
            json=transfer_payload,
        )

        self.assertEqual(
            response.status_code,
            200,
        )

        self.assertEqual(
            response.json()["status"],
            "ok",
        )

        with Session(engine) as session:
            cycle = session.get(
                Cycle,
                self.cycle_id,
            )

            self.assertIsNotNone(cycle)
            self.assertIsNone(
                cycle.status_idpk
            )

            request = session.exec(
                select(OutboundMessage)
                .where(
                    OutboundMessage.message_type == "request"
                )
                .where(
                    OutboundMessage.request_ask
                    == "status-statement"
                )
                .where(
                    OutboundMessage.resolved_at.is_(None)
                )
            ).first()

            self.assertIsNotNone(request)

            self.assertEqual(
                request.status,
                "PENDING",
            )

            self.assertTrue(
                request.dispatch_required
            )

            self.assertEqual(
                request.payload["type"],
                "request",
            )

            self.assertEqual(
                request.payload["data"]["ask"],
                "status-statement",
            )

            self.assertNotIn(
                "cycleId",
                request.payload,
            )


if __name__ == "__main__":
    unittest.main()