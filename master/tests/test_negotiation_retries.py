import unittest

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from sqlmodel import Session, select

from app.database import engine, run_migrations
from app.models import Cycle, Negotiation, OutboundMessage

from app.services.cycle_scheduler import CYCLE_NEGOTIATING, CYCLE_REPORT_WINDOW
from app.services.negotiation_proposals import (
    enqueue_timed_out_negotiation_retries,
)
from app.services.negotiation_state import (
    NEGOTIATION_PENDING_PUBLICATION,
    NEGOTIATION_TIMEOUT,
)


class NegotiationRetryTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        run_migrations()

    def setUp(self):
        self.connection = engine.connect()
        self.transaction = self.connection.begin()
        self.session = Session(bind=self.connection)

        self.now = datetime(
            2026,
            9,
            27,
            12,
            0,
            0,
            tzinfo=timezone.utc,
        )

        self.cycle_id = f"retry-{uuid4()}"

        cycle = Cycle(
            cycle_id=self.cycle_id,
            valid_until=self.now + timedelta(minutes=10),
            scheduler_state=CYCLE_NEGOTIATING,
            opening_budget_balance=Decimal("1000.00"),
            opening_energy_balance=Decimal("100.00"),
            budget_balance=Decimal("1000.00"),
            energy_balance=Decimal("100.00"),
            last_sequence=0,
            status_payload={},
            created_at=self.now,
        )

        self.session.add(cycle)
        self.session.flush()

    def tearDown(self):
        self.session.close()
        self.transaction.rollback()
        self.connection.close()

    def _create_timed_out_negotiation(self):
        idpk = str(uuid4())
        old_msg_id = str(uuid4())

        negotiation = Negotiation(
            cycle_id=self.cycle_id,
            idpk=idpk,
            latest_msg_id=old_msg_id,
            direction="give",
            requested_quantity=Decimal("10.00"),
            offered_price=Decimal("220.00"),
            status=NEGOTIATION_TIMEOUT,
            deadline_at=self.now - timedelta(seconds=1),
            created_at=self.now,
            updated_at=self.now,
        )

        self.session.add(negotiation)
        self.session.flush()

        return negotiation, idpk, old_msg_id

    def test_timeout_creates_retry_with_same_idpk_and_new_msg_id(
        self,
    ):
        negotiation, original_idpk, old_msg_id = (
            self._create_timed_out_negotiation()
        )

        retries = enqueue_timed_out_negotiation_retries(
            self.session,
            city_id="test-city",
            routing_key="central.test",
            now=self.now,
        )

        self.assertEqual(retries, 1)

        self.assertEqual(
            negotiation.idpk,
            original_idpk,
        )

        self.assertNotEqual(
            negotiation.latest_msg_id,
            old_msg_id,
        )

        self.assertEqual(
            negotiation.status,
            NEGOTIATION_PENDING_PUBLICATION,
        )

        self.assertIsNone(
            negotiation.deadline_at
        )

        outbound = self.session.exec(
            select(OutboundMessage).where(
                OutboundMessage.msg_id
                == negotiation.latest_msg_id
            )
        ).one()

        self.assertEqual(
            outbound.idpk,
            original_idpk,
        )

        self.assertEqual(
            outbound.message_type,
            "negotiation-proposal",
        )

        self.assertTrue(
            outbound.dispatch_required
        )

        self.assertEqual(
            outbound.payload["idpk"],
            original_idpk,
        )

        self.assertEqual(
            outbound.payload["msgId"],
            negotiation.latest_msg_id,
        )

    def test_retry_processing_is_idempotent_until_next_timeout(
        self,
    ):
        negotiation, _, _ = (
            self._create_timed_out_negotiation()
        )

        first = enqueue_timed_out_negotiation_retries(
            self.session,
            city_id="test-city",
            routing_key="central.test",
            now=self.now,
        )

        first_msg_id = negotiation.latest_msg_id

        second = enqueue_timed_out_negotiation_retries(
            self.session,
            city_id="test-city",
            routing_key="central.test",
            now=self.now,
        )

        self.assertEqual(first, 1)
        self.assertEqual(second, 0)

        self.assertEqual(
            negotiation.latest_msg_id,
            first_msg_id,
        )

        outbounds = self.session.exec(
            select(OutboundMessage).where(
                OutboundMessage.idpk
                == negotiation.idpk,
                OutboundMessage.message_type
                == "negotiation-proposal",
            )
        ).all()

        self.assertEqual(
            len(outbounds),
            1,
        )

    def test_expired_cycle_does_not_retry(
        self,
    ):
        negotiation, _, old_msg_id = (
            self._create_timed_out_negotiation()
        )

        cycle = self.session.get(
            Cycle,
            self.cycle_id,
        )

        cycle.valid_until = (
            self.now - timedelta(seconds=1)
        )

        self.session.add(cycle)
        self.session.flush()

        retries = enqueue_timed_out_negotiation_retries(
            self.session,
            city_id="test-city",
            routing_key="central.test",
            now=self.now,
        )

        self.assertEqual(retries, 0)

        self.assertEqual(
            negotiation.status,
            NEGOTIATION_TIMEOUT,
        )

        self.assertEqual(
            negotiation.latest_msg_id,
            old_msg_id,
        )

        outbounds = self.session.exec(
            select(OutboundMessage).where(
                OutboundMessage.idpk
                == negotiation.idpk
            )
        ).all()

        self.assertEqual(
            len(outbounds),
            0,
        )

    def test_timeout_retries_during_report_window(
        self,
    ):
        negotiation, original_idpk, old_msg_id = (
            self._create_timed_out_negotiation()
        )

        cycle = self.session.get(
            Cycle,
            self.cycle_id,
        )

        cycle.scheduler_state = (
            CYCLE_REPORT_WINDOW
        )

        cycle.valid_until = (
            self.now
            + timedelta(minutes=3)
        )

        self.session.add(cycle)
        self.session.flush()

        retries = (
            enqueue_timed_out_negotiation_retries(
                self.session,
                city_id="test-city",
                routing_key="central.test",
                now=self.now,
            )
        )

        self.assertEqual(
            retries,
            1,
        )

        self.assertEqual(
            negotiation.idpk,
            original_idpk,
        )

        self.assertNotEqual(
            negotiation.latest_msg_id,
            old_msg_id,
        )

        self.assertEqual(
            negotiation.status,
            NEGOTIATION_PENDING_PUBLICATION,
        )

if __name__ == "__main__":
    unittest.main()