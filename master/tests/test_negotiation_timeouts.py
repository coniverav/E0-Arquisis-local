import unittest

from datetime import (
    datetime,
    timedelta,
    timezone,
)

from decimal import Decimal
from uuid import uuid4

from sqlmodel import (
    Session,
    select,
)

from app.database import (
    engine,
    run_migrations,
)

from app.models import (
    Cycle,
    LedgerEntry,
    Negotiation,
)

from app.services.negotiation_state import (
    NEGOTIATION_ACKNOWLEDGED,
    NEGOTIATION_CONFIRMED,
    NEGOTIATION_PAID,
    NEGOTIATION_PENDING_PUBLICATION,
    NEGOTIATION_PROPOSED,
    NEGOTIATION_REJECTED,
    NEGOTIATION_TIMEOUT,
    NEGOTIATION_TIMEOUT_SECONDS,
    transition_negotiation,
)

from app.services.negotiation_timeouts import (
    process_expired_negotiations,
)


class NegotiationTimeoutTests(
    unittest.TestCase
):

    @classmethod
    def setUpClass(cls):

        run_migrations()

    def setUp(self):

        self.connection = (
            engine.connect()
        )

        self.transaction = (
            self.connection.begin()
        )

        self.session = Session(
            bind=self.connection
        )

        # Hora fija para que los tests sean
        # completamente deterministas.
        self.now = datetime(
            2026,
            9,
            27,
            12,
            0,
            0,
            tzinfo=timezone.utc,
        )

        self.cycle_id = (
            f"timeout-{uuid4()}"
        )

        cycle = Cycle(
            cycle_id=self.cycle_id,

            opening_budget_balance=(
                Decimal("100000.00")
            ),

            opening_energy_balance=(
                Decimal("50.00")
            ),

            budget_balance=(
                Decimal("100000.00")
            ),

            energy_balance=(
                Decimal("50.00")
            ),

            last_sequence=0,

            status_payload={},

            created_at=self.now,
        )

        self.session.add(
            cycle
        )

        self.session.flush()

    def tearDown(self):

        self.session.close()

        self.transaction.rollback()

        self.connection.close()

    # ========================================================
    # Helper
    # ========================================================

    def _create_negotiation(
        self,
        *,
        status: str,
        direction: str = "give",
        deadline_at: datetime | None = None,
    ) -> Negotiation:

        negotiation = Negotiation(
            cycle_id=self.cycle_id,

            idpk=str(
                uuid4()
            ),

            latest_msg_id=str(
                uuid4()
            ),

            direction=direction,

            requested_quantity=(
                Decimal("10.00")
            ),

            offered_price=(
                Decimal("210.00")
            ),

            status=status,

            deadline_at=deadline_at,

            created_at=self.now,

            updated_at=self.now,
        )

        self.session.add(
            negotiation
        )

        self.session.flush()

        return negotiation

    # ========================================================
    # 1. PROPOSED -> deadline +30 s
    # ========================================================

    def test_proposed_sets_deadline_exactly_30_seconds_later(
        self,
    ):

        negotiation = (
            self._create_negotiation(
                status=(
                    NEGOTIATION_PENDING_PUBLICATION
                ),
            )
        )

        changed = transition_negotiation(
            self.session,
            negotiation,
            NEGOTIATION_PROPOSED,
            now=self.now,
        )

        self.assertTrue(
            changed
        )

        self.assertEqual(
            negotiation.deadline_at,

            self.now
            + timedelta(
                seconds=(
                    NEGOTIATION_TIMEOUT_SECONDS
                )
            ),
        )

    # ========================================================
    # 2. ACK no reinicia plazo
    # ========================================================

    def test_ack_does_not_restart_proposal_deadline(
        self,
    ):

        negotiation = (
            self._create_negotiation(
                status=(
                    NEGOTIATION_PENDING_PUBLICATION
                ),
            )
        )

        transition_negotiation(
            self.session,
            negotiation,
            NEGOTIATION_PROPOSED,
            now=self.now,
        )

        original_deadline = (
            negotiation.deadline_at
        )

        transition_negotiation(
            self.session,
            negotiation,
            NEGOTIATION_ACKNOWLEDGED,

            now=(
                self.now
                + timedelta(seconds=10)
            ),
        )

        self.assertEqual(
            negotiation.deadline_at,
            original_deadline,
        )

    # ========================================================
    # 3. GIVE confirmado -> nuevo plazo para payment
    # ========================================================

    def test_give_confirmation_starts_new_30_second_payment_deadline(
        self,
    ):

        negotiation = (
            self._create_negotiation(
                status=NEGOTIATION_PROPOSED,

                direction="give",

                deadline_at=(
                    self.now
                    + timedelta(seconds=5)
                ),
            )
        )

        confirmation_time = (
            self.now
            + timedelta(seconds=20)
        )

        transition_negotiation(
            self.session,
            negotiation,
            NEGOTIATION_CONFIRMED,
            now=confirmation_time,
        )

        self.assertEqual(
            negotiation.deadline_at,

            confirmation_time
            + timedelta(
                seconds=(
                    NEGOTIATION_TIMEOUT_SECONDS
                )
            ),
        )

    # ========================================================
    # 4. TAKE confirmado no espera respuesta
    # ========================================================

    def test_take_confirmation_clears_deadline(
        self,
    ):

        negotiation = (
            self._create_negotiation(
                status=NEGOTIATION_PROPOSED,

                direction="take",

                deadline_at=(
                    self.now
                    + timedelta(seconds=5)
                ),
            )
        )

        transition_negotiation(
            self.session,
            negotiation,
            NEGOTIATION_CONFIRMED,

            now=(
                self.now
                + timedelta(seconds=2)
            ),
        )

        self.assertIsNone(
            negotiation.deadline_at
        )

    # ========================================================
    # 5. Estados finales limpian deadline
    # ========================================================

    def test_paid_and_rejected_clear_deadlines(
        self,
    ):

        paid_negotiation = (
            self._create_negotiation(
                status=NEGOTIATION_CONFIRMED,

                direction="give",

                deadline_at=(
                    self.now
                    + timedelta(seconds=20)
                ),
            )
        )

        transition_negotiation(
            self.session,
            paid_negotiation,
            NEGOTIATION_PAID,
            now=self.now,
        )

        self.assertIsNone(
            paid_negotiation.deadline_at
        )

        rejected_negotiation = (
            self._create_negotiation(
                status=NEGOTIATION_PROPOSED,

                direction="give",

                deadline_at=(
                    self.now
                    + timedelta(seconds=20)
                ),
            )
        )

        transition_negotiation(
            self.session,
            rejected_negotiation,
            NEGOTIATION_REJECTED,
            now=self.now,
        )

        self.assertIsNone(
            rejected_negotiation.deadline_at
        )

    # ========================================================
    # 6. PROPOSED vencido -> TIMEOUT
    # ========================================================

    def test_expired_proposed_negotiation_moves_to_timeout(
        self,
    ):

        deadline = (
            self.now
            - timedelta(seconds=1)
        )

        negotiation = (
            self._create_negotiation(
                status=NEGOTIATION_PROPOSED,
                deadline_at=deadline,
            )
        )

        processed = (
            process_expired_negotiations(
                self.session,
                now=self.now,
            )
        )

        self.assertEqual(
            processed,
            1,
        )

        self.assertEqual(
            negotiation.status,
            NEGOTIATION_TIMEOUT,
        )

        # Se conserva para trazabilidad.
        self.assertEqual(
            negotiation.deadline_at,
            deadline,
        )

        # Momento en que el worker detectó
        # el timeout.
        self.assertEqual(
            negotiation.updated_at,
            self.now,
        )

    # ========================================================
    # 7. ACKNOWLEDGED vencido -> TIMEOUT
    # ========================================================

    def test_expired_acknowledged_negotiation_moves_to_timeout(
        self,
    ):

        negotiation = (
            self._create_negotiation(
                status=(
                    NEGOTIATION_ACKNOWLEDGED
                ),

                deadline_at=(
                    self.now
                    - timedelta(seconds=1)
                ),
            )
        )

        processed = (
            process_expired_negotiations(
                self.session,
                now=self.now,
            )
        )

        self.assertEqual(
            processed,
            1,
        )

        self.assertEqual(
            negotiation.status,
            NEGOTIATION_TIMEOUT,
        )

    # ========================================================
    # 8. GIVE confirmado sin pago -> TIMEOUT
    # ========================================================

    def test_expired_confirmed_give_moves_to_timeout(
        self,
    ):

        negotiation = (
            self._create_negotiation(
                status=NEGOTIATION_CONFIRMED,

                direction="give",

                deadline_at=(
                    self.now
                    - timedelta(seconds=1)
                ),
            )
        )

        processed = (
            process_expired_negotiations(
                self.session,
                now=self.now,
            )
        )

        self.assertEqual(
            processed,
            1,
        )

        self.assertEqual(
            negotiation.status,
            NEGOTIATION_TIMEOUT,
        )

    # ========================================================
    # 9. Deadline futuro NO expira
    # ========================================================

    def test_future_deadline_does_not_timeout(
        self,
    ):

        deadline = (
            self.now
            + timedelta(seconds=1)
        )

        negotiation = (
            self._create_negotiation(
                status=NEGOTIATION_PROPOSED,
                deadline_at=deadline,
            )
        )

        processed = (
            process_expired_negotiations(
                self.session,
                now=self.now,
            )
        )

        self.assertEqual(
            processed,
            0,
        )

        self.assertEqual(
            negotiation.status,
            NEGOTIATION_PROPOSED,
        )

        self.assertEqual(
            negotiation.deadline_at,
            deadline,
        )

    # ========================================================
    # 10. TAKE confirmado nunca debe timeout
    # ========================================================

    def test_confirmed_take_is_not_timed_out_even_with_stale_deadline(
        self,
    ):

        negotiation = (
            self._create_negotiation(
                status=NEGOTIATION_CONFIRMED,

                direction="take",

                # Simula dato antiguo previo a E1-49.
                deadline_at=(
                    self.now
                    - timedelta(seconds=10)
                ),
            )
        )

        processed = (
            process_expired_negotiations(
                self.session,
                now=self.now,
            )
        )

        self.assertEqual(
            processed,
            0,
        )

        self.assertEqual(
            negotiation.status,
            NEGOTIATION_CONFIRMED,
        )

        # Se limpia el deadline antiguo.
        self.assertIsNone(
            negotiation.deadline_at
        )

    # ========================================================
    # 11. Estados terminales no cambian
    # ========================================================

    def test_terminal_states_are_not_timed_out(
        self,
    ):

        paid = (
            self._create_negotiation(
                status=NEGOTIATION_PAID,

                deadline_at=(
                    self.now
                    - timedelta(seconds=10)
                ),
            )
        )

        rejected = (
            self._create_negotiation(
                status=NEGOTIATION_REJECTED,

                deadline_at=(
                    self.now
                    - timedelta(seconds=10)
                ),
            )
        )

        processed = (
            process_expired_negotiations(
                self.session,
                now=self.now,
            )
        )

        self.assertEqual(
            processed,
            0,
        )

        self.assertEqual(
            paid.status,
            NEGOTIATION_PAID,
        )

        self.assertEqual(
            rejected.status,
            NEGOTIATION_REJECTED,
        )

    # ========================================================
    # 12. Timeout es idempotente
    # ========================================================

    def test_timeout_processing_is_idempotent(
        self,
    ):

        negotiation = (
            self._create_negotiation(
                status=NEGOTIATION_PROPOSED,

                deadline_at=(
                    self.now
                    - timedelta(seconds=1)
                ),
            )
        )

        first = (
            process_expired_negotiations(
                self.session,
                now=self.now,
            )
        )

        first_updated_at = (
            negotiation.updated_at
        )

        second = (
            process_expired_negotiations(
                self.session,

                now=(
                    self.now
                    + timedelta(seconds=5)
                ),
            )
        )

        self.assertEqual(
            first,
            1,
        )

        self.assertEqual(
            second,
            0,
        )

        self.assertEqual(
            negotiation.status,
            NEGOTIATION_TIMEOUT,
        )

        # El segundo procesamiento no toca
        # nuevamente la negociación.
        self.assertEqual(
            negotiation.updated_at,
            first_updated_at,
        )

    # ========================================================
    # 13. Varias expiradas en un mismo ciclo
    # ========================================================

    def test_worker_processes_multiple_expired_negotiations(
        self,
    ):

        first = (
            self._create_negotiation(
                status=NEGOTIATION_PROPOSED,

                deadline_at=(
                    self.now
                    - timedelta(seconds=1)
                ),
            )
        )

        second = (
            self._create_negotiation(
                status=(
                    NEGOTIATION_ACKNOWLEDGED
                ),

                deadline_at=(
                    self.now
                    - timedelta(seconds=2)
                ),
            )
        )

        future = (
            self._create_negotiation(
                status=NEGOTIATION_PROPOSED,

                deadline_at=(
                    self.now
                    + timedelta(seconds=10)
                ),
            )
        )

        processed = (
            process_expired_negotiations(
                self.session,
                now=self.now,
            )
        )

        self.assertEqual(
            processed,
            2,
        )

        self.assertEqual(
            first.status,
            NEGOTIATION_TIMEOUT,
        )

        self.assertEqual(
            second.status,
            NEGOTIATION_TIMEOUT,
        )

        self.assertEqual(
            future.status,
            NEGOTIATION_PROPOSED,
        )

    # ========================================================
    # 14. TIMEOUT no modifica ledger
    # ========================================================

    def test_timeout_does_not_modify_ledger_or_balances(
        self,
    ):

        negotiation = (
            self._create_negotiation(
                status=NEGOTIATION_PROPOSED,

                deadline_at=(
                    self.now
                    - timedelta(seconds=1)
                ),
            )
        )

        before_entries = (
            self.session.exec(
                select(LedgerEntry).where(
                    LedgerEntry.cycle_id
                    == self.cycle_id
                )
            ).all()
        )

        cycle = self.session.get(
            Cycle,
            self.cycle_id,
        )

        before_budget = (
            cycle.budget_balance
        )

        before_energy = (
            cycle.energy_balance
        )

        before_sequence = (
            cycle.last_sequence
        )

        processed = (
            process_expired_negotiations(
                self.session,
                now=self.now,
            )
        )

        after_entries = (
            self.session.exec(
                select(LedgerEntry).where(
                    LedgerEntry.cycle_id
                    == self.cycle_id
                )
            ).all()
        )

        self.assertEqual(
            processed,
            1,
        )

        self.assertEqual(
            negotiation.status,
            NEGOTIATION_TIMEOUT,
        )

        self.assertEqual(
            len(after_entries),
            len(before_entries),
        )

        self.assertEqual(
            cycle.budget_balance,
            before_budget,
        )

        self.assertEqual(
            cycle.energy_balance,
            before_energy,
        )

        self.assertEqual(
            cycle.last_sequence,
            before_sequence,
        )


if __name__ == "__main__":
    unittest.main()