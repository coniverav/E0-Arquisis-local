import unittest
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import delete
from sqlmodel import Session

from app.database import engine, run_migrations
from app.models import Cycle
from app.services.cycle_scheduler import (
    CYCLE_CLOSED,
    CYCLE_NEGOTIATING,
    CYCLE_PENDING,
    CYCLE_REPORT_WINDOW,
    configure_cycle_schedule,
    process_cycle_windows,
)


class CycleSchedulerTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        run_migrations()

    def setUp(self):
        self.cycle_ids = []

        self.now = datetime(
            2026,
            9,
            24,
            12,
            0,
            tzinfo=timezone.utc,
        )

    def tearDown(self):
        with Session(engine) as session:
            for cycle_id in self.cycle_ids:
                session.exec(
                    delete(Cycle).where(
                        Cycle.cycle_id == cycle_id
                    )
                )

            session.commit()

    def _create_cycle(
        self,
        *,
        valid_until=None,
        scheduler_state=CYCLE_PENDING,
    ):
        cycle_id = f"scheduler-test-{uuid4()}"
        self.cycle_ids.append(cycle_id)

        cycle = Cycle(
            cycle_id=cycle_id,
            created_at=self.now,
            scheduler_state=scheduler_state,
        )

        if valid_until is not None:
            configure_cycle_schedule(
                cycle,
                valid_until=valid_until,
            )

        with Session(engine) as session:
            session.add(cycle)
            session.commit()

        return cycle_id

    def test_configure_cycle_schedule(self):
        valid_until = self.now + timedelta(
            minutes=20
        )

        cycle_id = self._create_cycle(
            valid_until=valid_until,
        )

        with Session(engine) as session:
            cycle = session.get(
                Cycle,
                cycle_id,
            )

            self.assertEqual(
                cycle.scheduler_state,
                CYCLE_PENDING,
            )

            self.assertEqual(
                cycle.valid_until,
                valid_until,
            )

            self.assertEqual(
                cycle.report_window_opens_at,
                valid_until - timedelta(minutes=5),
            )

    def test_cycle_does_not_change_before_report_window(self):
        valid_until = self.now + timedelta(
            minutes=20
        )

        cycle_id = self._create_cycle(
            valid_until=valid_until,
        )

        with Session(engine) as session:
            result = process_cycle_windows(
                session,
                now=self.now + timedelta(minutes=10),
            )

            session.commit()

            cycle = session.get(
                Cycle,
                cycle_id,
            )

            self.assertEqual(
                cycle.scheduler_state,
                CYCLE_NEGOTIATING,
            )

            self.assertEqual(
                result["report_windows_opened"],
                0,
            )

            self.assertEqual(
                result["cycles_closed"],
                0,
            )

            self.assertEqual(
                result["cycles_prepared"],
                1,
            )

    def test_cycle_enters_report_window(self):
        valid_until = self.now + timedelta(
            minutes=20
        )

        cycle_id = self._create_cycle(
            valid_until=valid_until,
        )

        report_window_opens_at = (
            valid_until - timedelta(minutes=5)
        )

        with Session(engine) as session:
            result = process_cycle_windows(
                session,
                now=report_window_opens_at,
            )

            session.commit()

            cycle = session.get(
                Cycle,
                cycle_id,
            )

            self.assertEqual(
                cycle.scheduler_state,
                CYCLE_REPORT_WINDOW,
            )

            self.assertEqual(
                cycle.report_window_opened_at,
                report_window_opens_at,
            )

            self.assertEqual(
                result["report_windows_opened"],
                1,
            )

    def test_cycle_closes_at_valid_until(self):
        valid_until = self.now + timedelta(
            minutes=20
        )

        cycle_id = self._create_cycle(
            valid_until=valid_until,
        )

        with Session(engine) as session:
            result = process_cycle_windows(
                session,
                now=valid_until,
            )

            session.commit()

            cycle = session.get(
                Cycle,
                cycle_id,
            )

            self.assertEqual(
                cycle.scheduler_state,
                CYCLE_CLOSED,
            )

            self.assertEqual(
                cycle.closed_at,
                valid_until,
            )

            self.assertEqual(
                cycle.report_window_opened_at,
                valid_until - timedelta(minutes=5),
            )

            self.assertEqual(
                result["cycles_closed"],
                1,
            )

    def test_scheduler_is_idempotent(self):
        valid_until = self.now + timedelta(
            minutes=20
        )

        cycle_id = self._create_cycle(
            valid_until=valid_until,
        )

        report_time = (
            valid_until - timedelta(minutes=5)
        )

        with Session(engine) as session:
            first = process_cycle_windows(
                session,
                now=report_time,
            )
            session.commit()

        with Session(engine) as session:
            second = process_cycle_windows(
                session,
                now=report_time + timedelta(seconds=10),
            )
            session.commit()

            cycle = session.get(
                Cycle,
                cycle_id,
            )

            self.assertEqual(
                cycle.scheduler_state,
                CYCLE_REPORT_WINDOW,
            )

            self.assertEqual(
                first["report_windows_opened"],
                1,
            )

            self.assertEqual(
                second["report_windows_opened"],
                0,
            )

    def test_scheduler_processes_multiple_cycles(self):
        first_valid_until = (
            self.now + timedelta(minutes=20)
        )

        second_valid_until = (
            self.now + timedelta(minutes=10)
        )

        first_cycle_id = self._create_cycle(
            valid_until=first_valid_until,
        )

        second_cycle_id = self._create_cycle(
            valid_until=second_valid_until,
        )

        with Session(engine) as session:
            result = process_cycle_windows(
                session,
                now=self.now + timedelta(minutes=15),
            )

            session.commit()

            first_cycle = session.get(
                Cycle,
                first_cycle_id,
            )

            second_cycle = session.get(
                Cycle,
                second_cycle_id,
            )

            self.assertEqual(
                first_cycle.scheduler_state,
                CYCLE_REPORT_WINDOW,
            )

            self.assertEqual(
                second_cycle.scheduler_state,
                CYCLE_CLOSED,
            )

            self.assertEqual(
                result["report_windows_opened"],
                1,
            )

            self.assertEqual(
                result["cycles_closed"],
                1,
            )


if __name__ == "__main__":
    unittest.main()