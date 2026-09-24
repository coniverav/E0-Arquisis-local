import unittest
from datetime import (
    datetime,
    timedelta,
    timezone,
)
from decimal import Decimal
from uuid import uuid4

from sqlmodel import Session, select

from app.database import engine, run_migrations
from app.models import (
    Cycle,
    NegotiationReport,
    OutboundMessage,
)
from app.services.negotiation_report_dispatch import (
    enqueue_due_negotiation_reports,
    mark_negotiation_report_sent,
)


class NegotiationReportDispatchTests(
    unittest.TestCase
):

    @classmethod
    def setUpClass(cls):
        run_migrations()

    def setUp(self):
        self.connection = engine.connect()
        self.transaction = self.connection.begin()

        self.session = Session(
            bind=self.connection
        )

        self.now = datetime.now(
            timezone.utc
        )

        self.cycle_id = (
            f"report-dispatch-{uuid4()}"
        )

        cycle = Cycle(
            cycle_id=self.cycle_id,
            scheduler_state="REPORT_WINDOW",
            valid_until=(
                self.now
                + timedelta(minutes=2)
            ),
            report_window_opens_at=(
                self.now
                - timedelta(minutes=3)
            ),
            report_window_opened_at=self.now,
            opening_budget_balance=Decimal(
                "1000.00"
            ),
            opening_energy_balance=Decimal(
                "50.00"
            ),
            budget_balance=Decimal(
                "1000.00"
            ),
            energy_balance=Decimal(
                "50.00"
            ),
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

    def test_report_is_generated_and_enqueued_in_window(
        self,
    ):
        count = enqueue_due_negotiation_reports(
            self.session,
            city_id="TEST",
            routing_key="central",
            now=self.now,
        )

        self.assertEqual(count, 1)

        report = self.session.exec(
            select(NegotiationReport).where(
                NegotiationReport.cycle_id
                == self.cycle_id
            )
        ).one()

        outbound = self.session.exec(
            select(OutboundMessage).where(
                OutboundMessage.msg_id
                == report.msg_id
            )
        ).one()

        self.assertEqual(
            outbound.message_type,
            "negotiation-report",
        )

        self.assertEqual(
            outbound.status,
            "PENDING",
        )

        self.assertTrue(
            outbound.dispatch_required
        )

        self.assertEqual(
            outbound.routing_key,
            "central",
        )

        self.assertEqual(
            outbound.payload,
            report.payload,
        )

    def test_report_is_not_enqueued_twice(self):
        first = enqueue_due_negotiation_reports(
            self.session,
            city_id="TEST",
            routing_key="central",
            now=self.now,
        )

        second = enqueue_due_negotiation_reports(
            self.session,
            city_id="TEST",
            routing_key="central",
            now=self.now,
        )

        self.assertEqual(first, 1)
        self.assertEqual(second, 0)

        reports = self.session.exec(
            select(NegotiationReport).where(
                NegotiationReport.cycle_id
                == self.cycle_id
            )
        ).all()

        outbounds = self.session.exec(
            select(OutboundMessage).where(
                OutboundMessage.cycle_id
                == self.cycle_id,
                OutboundMessage.message_type
                == "negotiation-report",
            )
        ).all()

        self.assertEqual(len(reports), 1)
        self.assertEqual(len(outbounds), 1)

    def test_report_outside_window_is_not_enqueued(
        self,
    ):
        cycle = self.session.get(
            Cycle,
            self.cycle_id,
        )

        cycle.scheduler_state = "NEGOTIATING"

        self.session.add(cycle)
        self.session.flush()

        count = enqueue_due_negotiation_reports(
            self.session,
            city_id="TEST",
            routing_key="central",
            now=self.now,
        )

        self.assertEqual(count, 0)

    def test_published_report_records_sent_at(self):
        enqueue_due_negotiation_reports(
            self.session,
            city_id="TEST",
            routing_key="central",
            now=self.now,
        )

        report = self.session.exec(
            select(NegotiationReport).where(
                NegotiationReport.cycle_id
                == self.cycle_id
            )
        ).one()

        sent_at = (
            self.now
            + timedelta(seconds=1)
        )

        updated = mark_negotiation_report_sent(
            self.session,
            msg_id=report.msg_id,
            sent_at=sent_at,
        )

        self.assertIsNotNone(updated)
        self.assertEqual(
            updated.sent_at,
            sent_at,
        )


if __name__ == "__main__":
    unittest.main()
