import unittest
from uuid import uuid4

from sqlalchemy import delete
from sqlmodel import Session

from app.database import engine, run_migrations
from app.models import InboundMessage, OutboundMessage
from app.services.message_audit import (
    INBOUND_DUPLICATE,
    INBOUND_PROCESSED,
    OUTBOUND_FAILED,
    OUTBOUND_PUBLISHED,
    record_inbound_message,
    mark_inbound_result,
    record_outbound_message,
    mark_outbound_failed,
    mark_outbound_published,
)


class MessageAuditTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        run_migrations()

    def setUp(self):
        with Session(engine) as session:
            session.exec(delete(OutboundMessage))
            session.exec(delete(InboundMessage))
            session.commit()

    def test_inbound_message_is_persisted(self):
        msg_id = str(uuid4())
        idpk = str(uuid4())

        with Session(engine) as session:
            message = record_inbound_message(
                session,
                msg_id=msg_id,
                idpk=idpk,
                message_type="status-statement",
                payload={"cycleId": "cycle-test"},
                cycle_id="cycle-test",
                sender="central",
            )

            self.assertIsNotNone(message.id)
            self.assertEqual(message.status, "RECEIVED")

        with Session(engine) as session:
            stored = session.get(InboundMessage, message.id)

            self.assertIsNotNone(stored)
            self.assertEqual(stored.msg_id, msg_id)
            self.assertEqual(stored.idpk, idpk)

    def test_duplicate_msg_id_can_be_audited_twice(self):
        msg_id = str(uuid4())
        idpk = str(uuid4())

        with Session(engine) as session:
            original = record_inbound_message(
                session,
                msg_id=msg_id,
                idpk=idpk,
                message_type="transfer",
                payload={"quantity": 100},
            )

            mark_inbound_result(
                session,
                original,
                status=INBOUND_PROCESSED,
            )

            duplicate = record_inbound_message(
                session,
                msg_id=msg_id,
                idpk=idpk,
                message_type="transfer",
                payload={"quantity": 100},
            )

            mark_inbound_result(
                session,
                duplicate,
                status=INBOUND_DUPLICATE,
                reason="Mensaje recibido nuevamente",
            )

            self.assertNotEqual(original.id, duplicate.id)
            self.assertEqual(duplicate.status, INBOUND_DUPLICATE)

    def test_outbound_message_lifecycle(self):
        with Session(engine) as session:
            message = record_outbound_message(
                session,
                msg_id=str(uuid4()),
                idpk=str(uuid4()),
                message_type="ack",
                payload={"type": "ack"},
                target_msg_id=str(uuid4()),
                routing_key="central.test",
            )

            self.assertEqual(message.status, "PENDING")
            self.assertEqual(message.attempt_count, 0)

            mark_outbound_published(session, message)

            self.assertEqual(message.status, OUTBOUND_PUBLISHED)
            self.assertEqual(message.attempt_count, 1)
            self.assertIsNotNone(message.published_at)

            # Repetir el mismo resultado no debe contar
            # como un segundo intento.
            mark_outbound_published(session, message)

            self.assertEqual(
                message.status,
                OUTBOUND_PUBLISHED,
            )
            self.assertEqual(
                message.attempt_count,
                1,
            )

    def test_failed_outbound_attempt_keeps_evidence(self):
        with Session(engine) as session:
            message = record_outbound_message(
                session,
                msg_id=str(uuid4()),
                idpk=str(uuid4()),
                message_type="ack",
                payload={"type": "ack"},
            )

            mark_outbound_failed(
                session,
                message,
                error="RabbitMQ unavailable",
            )

            self.assertEqual(message.status, OUTBOUND_FAILED)
            self.assertEqual(message.attempt_count, 1)
            self.assertEqual(message.last_error, "RabbitMQ unavailable")

            # Repetir exactamente el mismo fallo no debe
            # contabilizar otro intento.
            mark_outbound_failed(
                session,
                message,
                error="RabbitMQ unavailable",
            )

            self.assertEqual(
                message.attempt_count,
                1,
            )


if __name__ == "__main__":
    unittest.main()
