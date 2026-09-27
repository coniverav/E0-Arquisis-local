import unittest

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import delete
from sqlmodel import Session, select

from app.database import engine, run_migrations

from app.models import (
    Cycle,
    LedgerEntry,
    Negotiation,
    OutboundMessage,
)

from app.services.negotiation_payments import (
    NegotiationPaymentError,
    enqueue_take_payment,
    process_give_payment,
)

from app.services.negotiation_state import (
    NEGOTIATION_CONFIRMED,
    NEGOTIATION_PAID,
    NEGOTIATION_PROPOSED,
)


class NegotiationPaymentTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        """
        Ejecuta las migraciones antes de comenzar los tests.
        """
        run_migrations()

    def setUp(self):
        """
        Cada test usa identificadores únicos para no interferir
        con otros tests ni con datos existentes.
        """

        self.cycle_id = (
            f"payment-test-{uuid4()}"
        )

        self.negotiation_idpk = str(uuid4())

        # Corresponde al msgId del give/take recibido
        # desde la central.
        self.confirmation_msg_id = str(uuid4())

        # Para transfers recibidos.
        self.transfer_idpk = str(uuid4())
        self.transfer_msg_id = str(uuid4())

        self.city_id = "KLD"
        self.routing_key = "central"

    def tearDown(self):
        """
        Limpia exclusivamente los datos creados
        para este cycle_id.
        """

        with Session(engine) as session:

            # Outbound primero porque puede referenciar
            # información del ciclo/negociación.
            session.exec(
                delete(OutboundMessage).where(
                    OutboundMessage.cycle_id
                    == self.cycle_id
                )
            )

            session.exec(
                delete(LedgerEntry).where(
                    LedgerEntry.cycle_id
                    == self.cycle_id
                )
            )

            session.exec(
                delete(Negotiation).where(
                    Negotiation.cycle_id
                    == self.cycle_id
                )
            )

            cycle = session.get(
                Cycle,
                self.cycle_id,
            )

            if cycle is not None:
                session.delete(cycle)

            session.commit()

    # ============================================================
    # HELPERS
    # ============================================================

    def _create_cycle_and_negotiation(
        self,
        *,
        direction: str,
        budget_balance: Decimal = Decimal("100000.00"),
        energy_balance: Decimal = Decimal("50.00"),
        confirmed_energy: Decimal = Decimal("40.00"),
        confirmed_price: Decimal = Decimal("210.00"),
        status: str = NEGOTIATION_CONFIRMED,
    ) -> int:

        now = datetime.now(timezone.utc)

        with Session(engine) as session:

            cycle = Cycle(
                cycle_id=self.cycle_id,

                status_msg_id=str(uuid4()),
                status_idpk=str(uuid4()),

                generation_capacity=Decimal("150.00"),
                consumption=Decimal("100.00"),
                generation_cost=Decimal("210.00"),

                valid_until=(
                    now + timedelta(minutes=10)
                ),

                scheduler_state="NEGOTIATING",

                status_payload={
                    "energy": {
                        "generationCapacity": 150,
                        "consumption": 100,
                        "generationCost": 210,
                    }
                },

                opening_budget_balance=budget_balance,
                opening_energy_balance=energy_balance,

                budget_balance=budget_balance,
                energy_balance=energy_balance,

                last_sequence=0,

                created_at=now,
            )

            session.add(cycle)
            session.flush()

            negotiation = Negotiation(
                cycle_id=self.cycle_id,
                idpk=self.negotiation_idpk,

                # Después de E1-45/E1-46,
                # latest_msg_id apunta a la confirmación
                # give/take.
                latest_msg_id=self.confirmation_msg_id,

                direction=direction,

                requested_quantity=confirmed_energy,

                offered_price=confirmed_price,

                status=status,

                confirmed_energy=confirmed_energy,
                confirmed_price=confirmed_price,

                payment_quantity=None,

                deadline_at=(
                    now + timedelta(seconds=30)
                ),

                created_at=now,
                updated_at=now,
            )

            session.add(negotiation)
            session.commit()
            session.refresh(negotiation)

            return negotiation.id

    def _get_negotiation(self) -> Negotiation:

        with Session(engine) as session:

            negotiation = session.exec(
                select(Negotiation).where(
                    Negotiation.cycle_id
                    == self.cycle_id
                )
            ).one()

            session.expunge(negotiation)

            return negotiation

    def _assert_no_payment_effect(
        self,
        *,
        expected_budget: Decimal,
        expected_energy: Decimal,
    ) -> None:

        with Session(engine) as session:

            cycle = session.get(
                Cycle,
                self.cycle_id,
            )

            entries = session.exec(
                select(LedgerEntry).where(
                    LedgerEntry.cycle_id
                    == self.cycle_id,
                )
            ).all()

            outbound = session.exec(
                select(OutboundMessage).where(
                    OutboundMessage.cycle_id
                    == self.cycle_id,
                )
            ).all()

            negotiation = session.exec(
                select(Negotiation).where(
                    Negotiation.cycle_id
                    == self.cycle_id
                )
            ).one()

            self.assertEqual(
                cycle.budget_balance,
                expected_budget,
            )

            self.assertEqual(
                cycle.energy_balance,
                expected_energy,
            )

            self.assertEqual(
                len(entries),
                0,
            )

            self.assertEqual(
                len(outbound),
                0,
            )

            self.assertIsNone(
                negotiation.payment_quantity
            )

    # ============================================================
    # E1-47
    # TAKE -> EMITIR TRANSFER DE PAGO
    # ============================================================

    def test_take_payment_creates_transfer_and_decreases_budget(
        self,
    ):

        """
        Caso feliz principal de E1-47.

        TAKE:
            energy = 40
            price = 210

            payment = 8400

        Budget:
            100000 -> 91600
        """

        negotiation_id = (
            self._create_cycle_and_negotiation(
                direction="take",
                budget_balance=Decimal("100000.00"),
                energy_balance=Decimal("50.00"),
                confirmed_energy=Decimal("40.00"),
                confirmed_price=Decimal("210.00"),
            )
        )

        with Session(engine) as session:

            (
                negotiation,
                outbound,
                ledger_entry,
                applied,
            ) = enqueue_take_payment(
                session,
                negotiation_id=negotiation_id,
                city_id=self.city_id,
                routing_key=self.routing_key,
            )

            self.assertTrue(applied)

            session.commit()

            transfer_msg_id = outbound.msg_id

        with Session(engine) as session:

            cycle = session.get(
                Cycle,
                self.cycle_id,
            )

            negotiation = session.get(
                Negotiation,
                negotiation_id,
            )

            entry = session.exec(
                select(LedgerEntry).where(
                    LedgerEntry.cycle_id
                    == self.cycle_id,
                    LedgerEntry.operation_type
                    == "PAYMENT_SENT",
                )
            ).one()

            outbound = session.exec(
                select(OutboundMessage).where(
                    OutboundMessage.cycle_id
                    == self.cycle_id,
                    OutboundMessage.message_type
                    == "transfer",
                )
            ).one()

            # ----------------------------------------
            # Ledger
            # ----------------------------------------

            self.assertEqual(
                entry.budget_delta,
                Decimal("-8400.00"),
            )

            self.assertEqual(
                entry.energy_delta,
                Decimal("0.00"),
            )

            self.assertEqual(
                cycle.budget_balance,
                Decimal("91600.00"),
            )

            # El pago NO vuelve a modificar energía.
            self.assertEqual(
                cycle.energy_balance,
                Decimal("50.00"),
            )

            self.assertEqual(
                entry.negotiation_id,
                negotiation.id,
            )

            # ----------------------------------------
            # Negotiation
            # ----------------------------------------

            self.assertEqual(
                negotiation.payment_quantity,
                Decimal("8400.00"),
            )

            # Hasta que el connector confirme que fue
            # publicado, sigue CONFIRMED.
            self.assertEqual(
                negotiation.status,
                NEGOTIATION_CONFIRMED,
            )

            self.assertEqual(
                negotiation.latest_msg_id,
                transfer_msg_id,
            )

            # ----------------------------------------
            # Outbound transfer
            # ----------------------------------------

            self.assertEqual(
                outbound.message_type,
                "transfer",
            )

            self.assertTrue(
                outbound.dispatch_required
            )

            self.assertEqual(
                outbound.routing_key,
                self.routing_key,
            )

            self.assertEqual(
                outbound.payload["type"],
                "transfer",
            )

            self.assertEqual(
                outbound.payload["cityId"],
                self.city_id,
            )

            self.assertEqual(
                outbound.payload["cycleId"],
                self.cycle_id,
            )

            # becauseOf debe apuntar al msgId
            # de la confirmación TAKE.
            self.assertEqual(
                outbound.payload["data"]["becauseOf"],
                self.confirmation_msg_id,
            )

            self.assertEqual(
                Decimal(
                    str(
                        outbound.payload["data"][
                            "quantity"
                        ]
                    )
                ),
                Decimal("8400"),
            )

    # ============================================================
    # E1-47
    # NO DEBE PAGAR DOS VECES
    # ============================================================

    def test_take_payment_is_not_created_twice(
        self,
    ):

        negotiation_id = (
            self._create_cycle_and_negotiation(
                direction="take",
                budget_balance=Decimal("100000.00"),
                confirmed_energy=Decimal("40.00"),
                confirmed_price=Decimal("210.00"),
            )
        )

        # Primera ejecución.
        with Session(engine) as session:

            _, _, _, applied = (
                enqueue_take_payment(
                    session,
                    negotiation_id=negotiation_id,
                    city_id=self.city_id,
                    routing_key=self.routing_key,
                )
            )

            self.assertTrue(applied)

            session.commit()

        # Segunda ejecución del mismo pago.
        with Session(engine) as session:

            _, _, _, applied = (
                enqueue_take_payment(
                    session,
                    negotiation_id=negotiation_id,
                    city_id=self.city_id,
                    routing_key=self.routing_key,
                )
            )

            self.assertFalse(applied)

            session.commit()

        with Session(engine) as session:

            cycle = session.get(
                Cycle,
                self.cycle_id,
            )

            entries = session.exec(
                select(LedgerEntry).where(
                    LedgerEntry.cycle_id
                    == self.cycle_id,
                    LedgerEntry.operation_type
                    == "PAYMENT_SENT",
                )
            ).all()

            outbound = session.exec(
                select(OutboundMessage).where(
                    OutboundMessage.cycle_id
                    == self.cycle_id,
                    OutboundMessage.message_type
                    == "transfer",
                )
            ).all()

            # Solo una modificación monetaria.
            self.assertEqual(
                len(entries),
                1,
            )

            # Solo un mensaje transfer.
            self.assertEqual(
                len(outbound),
                1,
            )

            # Solo se descontaron 8400 una vez.
            self.assertEqual(
                cycle.budget_balance,
                Decimal("91600.00"),
            )

            self.assertEqual(
                cycle.last_sequence,
                1,
            )

    # ============================================================
    # E1-47
    # REDONDEO DEL PAGO
    # ============================================================

    def test_take_payment_rounds_total_to_two_decimals(
        self,
    ):

        """
        1.5 * 2.67 = 4.005

        Con round2:
            4.01
        """

        negotiation_id = (
            self._create_cycle_and_negotiation(
                direction="take",
                budget_balance=Decimal("100.00"),
                confirmed_energy=Decimal("1.50"),
                confirmed_price=Decimal("2.67"),
            )
        )

        with Session(engine) as session:

            _, outbound, entry, applied = (
                enqueue_take_payment(
                    session,
                    negotiation_id=negotiation_id,
                    city_id=self.city_id,
                    routing_key=self.routing_key,
                )
            )

            self.assertTrue(applied)

            self.assertEqual(
                entry.budget_delta,
                Decimal("-4.01"),
            )

            self.assertEqual(
                Decimal(
                    str(
                        outbound.payload["data"][
                            "quantity"
                        ]
                    )
                ),
                Decimal("4.01"),
            )

            session.commit()

    # ============================================================
    # E1-47
    # BUDGET PUEDE QUEDAR NEGATIVO
    # ============================================================

    def test_take_payment_can_leave_negative_budget(
        self,
    ):

        negotiation_id = (
            self._create_cycle_and_negotiation(
                direction="take",
                budget_balance=Decimal("1000.00"),
                confirmed_energy=Decimal("10.00"),
                confirmed_price=Decimal("210.00"),
            )
        )

        with Session(engine) as session:

            enqueue_take_payment(
                session,
                negotiation_id=negotiation_id,
                city_id=self.city_id,
                routing_key=self.routing_key,
            )

            session.commit()

        with Session(engine) as session:

            cycle = session.get(
                Cycle,
                self.cycle_id,
            )

            # 1000 - 2100
            self.assertEqual(
                cycle.budget_balance,
                Decimal("-1100.00"),
            )

    # ============================================================
    # E1-47
    # GIVE NO DEBE EMITIR PAGO
    # ============================================================

    def test_take_payment_rejects_give_negotiation(
        self,
    ):

        negotiation_id = (
            self._create_cycle_and_negotiation(
                direction="give",
            )
        )

        with Session(engine) as session:

            with self.assertRaises(
                NegotiationPaymentError
            ):
                enqueue_take_payment(
                    session,
                    negotiation_id=negotiation_id,
                    city_id=self.city_id,
                    routing_key=self.routing_key,
                )

            session.rollback()

        self._assert_no_payment_effect(
            expected_budget=Decimal("100000.00"),
            expected_energy=Decimal("50.00"),
        )

    # ============================================================
    # E1-47
    # NEGOCIACIÓN NO CONFIRMADA
    # ============================================================

    def test_take_payment_rejects_unconfirmed_negotiation(
        self,
    ):

        negotiation_id = (
            self._create_cycle_and_negotiation(
                direction="take",
                status=NEGOTIATION_PROPOSED,
            )
        )

        with Session(engine) as session:

            with self.assertRaises(
                NegotiationPaymentError
            ):
                enqueue_take_payment(
                    session,
                    negotiation_id=negotiation_id,
                    city_id=self.city_id,
                    routing_key=self.routing_key,
                )

            session.rollback()

        self._assert_no_payment_effect(
            expected_budget=Decimal("100000.00"),
            expected_energy=Decimal("50.00"),
        )

    # ============================================================
    # E1-47
    # NEGOCIACIÓN INEXISTENTE
    # ============================================================

    def test_take_payment_rejects_unknown_negotiation(
        self,
    ):

        self._create_cycle_and_negotiation(
            direction="take",
        )

        with Session(engine) as session:

            with self.assertRaises(
                NegotiationPaymentError
            ):
                enqueue_take_payment(
                    session,
                    negotiation_id=999999999,
                    city_id=self.city_id,
                    routing_key=self.routing_key,
                )

            session.rollback()

    # ============================================================
    # E1-48
    # GIVE -> RECIBIR PAGO
    # ============================================================

    def test_give_payment_increases_budget_and_marks_paid(
        self,
    ):

        """
        GIVE:
            energy = 40
            price = 220.50

            payment = 8820

        Budget:
            100000 -> 108820
        """

        negotiation_id = (
            self._create_cycle_and_negotiation(
                direction="give",
                budget_balance=Decimal("100000.00"),
                energy_balance=Decimal("60.00"),
                confirmed_energy=Decimal("40.00"),
                confirmed_price=Decimal("220.50"),
            )
        )

        details = {
            "idpk": self.transfer_idpk,
            "msgId": self.transfer_msg_id,
            "type": "transfer",
            "cycleId": self.cycle_id,
            "sender": "central",
            "data": {
                "becauseOf": self.confirmation_msg_id,
                "quantity": 8820,
            },
        }

        with Session(engine) as session:

            (
                negotiation,
                entry,
                applied,
            ) = process_give_payment(
                session,

                cycle_id=self.cycle_id,

                idpk=self.transfer_idpk,
                msg_id=self.transfer_msg_id,

                because_of=self.confirmation_msg_id,

                quantity=Decimal("8820.00"),

                details=details,
            )

            self.assertTrue(applied)

            session.commit()

        with Session(engine) as session:

            cycle = session.get(
                Cycle,
                self.cycle_id,
            )

            negotiation = session.get(
                Negotiation,
                negotiation_id,
            )

            entry = session.exec(
                select(LedgerEntry).where(
                    LedgerEntry.cycle_id
                    == self.cycle_id,
                    LedgerEntry.operation_type
                    == "PAYMENT_RECEIVED",
                )
            ).one()

            self.assertEqual(
                cycle.budget_balance,
                Decimal("108820.00"),
            )

            # No modifica energía.
            self.assertEqual(
                cycle.energy_balance,
                Decimal("60.00"),
            )

            self.assertEqual(
                entry.budget_delta,
                Decimal("8820.00"),
            )

            self.assertEqual(
                entry.energy_delta,
                Decimal("0.00"),
            )

            self.assertEqual(
                entry.negotiation_id,
                negotiation.id,
            )

            self.assertEqual(
                entry.source_msg_id,
                self.transfer_msg_id,
            )

            self.assertEqual(
                negotiation.payment_quantity,
                Decimal("8820.00"),
            )

            self.assertEqual(
                negotiation.status,
                NEGOTIATION_PAID,
            )

            # Para GIVE conservamos el msgId de la
            # confirmación, porque futuros transfers
            # duplicados usarán el mismo becauseOf.
            self.assertEqual(
                negotiation.latest_msg_id,
                self.confirmation_msg_id,
            )

    # ============================================================
    # E1-48
    # DUPLICADO CON MISMO IDPK
    # ============================================================

    def test_duplicate_give_payment_same_idpk_is_not_applied_twice(
        self,
    ):

        self._create_cycle_and_negotiation(
            direction="give",
            budget_balance=Decimal("100000.00"),
            confirmed_energy=Decimal("40.00"),
            confirmed_price=Decimal("220.50"),
        )

        with Session(engine) as session:

            _, _, applied = (
                process_give_payment(
                    session,

                    cycle_id=self.cycle_id,

                    idpk=self.transfer_idpk,
                    msg_id=self.transfer_msg_id,

                    because_of=self.confirmation_msg_id,

                    quantity=Decimal("8820.00"),

                    details={},
                )
            )

            self.assertTrue(applied)

            session.commit()

        # Mismo idpk.
        with Session(engine) as session:

            _, _, applied = (
                process_give_payment(
                    session,

                    cycle_id=self.cycle_id,

                    idpk=self.transfer_idpk,

                    # Incluso aunque tenga otro msgId.
                    msg_id=str(uuid4()),

                    because_of=self.confirmation_msg_id,

                    quantity=Decimal("8820.00"),

                    details={},
                )
            )

            self.assertFalse(applied)

            session.commit()

        with Session(engine) as session:

            cycle = session.get(
                Cycle,
                self.cycle_id,
            )

            entries = session.exec(
                select(LedgerEntry).where(
                    LedgerEntry.cycle_id
                    == self.cycle_id,
                    LedgerEntry.operation_type
                    == "PAYMENT_RECEIVED",
                )
            ).all()

            self.assertEqual(
                len(entries),
                1,
            )

            self.assertEqual(
                cycle.budget_balance,
                Decimal("108820.00"),
            )

            self.assertEqual(
                cycle.last_sequence,
                1,
            )

    # ============================================================
    # E1-48
    # DUPLICADO CON OTRO IDPK PERO MISMO becauseOf
    # ============================================================

    def test_duplicate_give_payment_different_idpk_same_becauseof_is_not_applied_twice(
        self,
    ):

        self._create_cycle_and_negotiation(
            direction="give",
            budget_balance=Decimal("100000.00"),
            confirmed_energy=Decimal("40.00"),
            confirmed_price=Decimal("220.50"),
        )

        # Primer pago.
        with Session(engine) as session:

            _, _, applied = (
                process_give_payment(
                    session,

                    cycle_id=self.cycle_id,

                    idpk=self.transfer_idpk,
                    msg_id=self.transfer_msg_id,

                    because_of=self.confirmation_msg_id,

                    quantity=Decimal("8820.00"),

                    details={},
                )
            )

            self.assertTrue(applied)

            session.commit()

        # Segundo mensaje completamente distinto,
        # pero intenta pagar la MISMA confirmación GIVE.
        with Session(engine) as session:

            _, _, applied = (
                process_give_payment(
                    session,

                    cycle_id=self.cycle_id,

                    idpk=str(uuid4()),
                    msg_id=str(uuid4()),

                    # MISMO becauseOf.
                    because_of=self.confirmation_msg_id,

                    quantity=Decimal("8820.00"),

                    details={},
                )
            )

            self.assertFalse(applied)

            session.commit()

        with Session(engine) as session:

            cycle = session.get(
                Cycle,
                self.cycle_id,
            )

            entries = session.exec(
                select(LedgerEntry).where(
                    LedgerEntry.cycle_id
                    == self.cycle_id,
                    LedgerEntry.operation_type
                    == "PAYMENT_RECEIVED",
                )
            ).all()

            self.assertEqual(
                len(entries),
                1,
            )

            # No se acreditan 8820 dos veces.
            self.assertEqual(
                cycle.budget_balance,
                Decimal("108820.00"),
            )

    # ============================================================
    # E1-48
    # MONTO INCORRECTO
    # ============================================================

    def test_give_payment_rejects_wrong_quantity(
        self,
    ):

        self._create_cycle_and_negotiation(
            direction="give",
            confirmed_energy=Decimal("40.00"),
            confirmed_price=Decimal("220.50"),
        )

        with Session(engine) as session:

            with self.assertRaises(
                NegotiationPaymentError
            ):
                process_give_payment(
                    session,

                    cycle_id=self.cycle_id,

                    idpk=self.transfer_idpk,
                    msg_id=self.transfer_msg_id,

                    because_of=self.confirmation_msg_id,

                    # Debería ser 8820.
                    quantity=Decimal("9999.00"),

                    details={},
                )

            session.rollback()

        self._assert_no_payment_effect(
            expected_budget=Decimal("100000.00"),
            expected_energy=Decimal("50.00"),
        )

    # ============================================================
    # E1-48
    # becauseOf DESCONOCIDO
    # ============================================================

    def test_give_payment_rejects_unknown_becauseof(
        self,
    ):

        self._create_cycle_and_negotiation(
            direction="give",
            confirmed_energy=Decimal("40.00"),
            confirmed_price=Decimal("220.50"),
        )

        with Session(engine) as session:

            with self.assertRaises(
                NegotiationPaymentError
            ):
                process_give_payment(
                    session,

                    cycle_id=self.cycle_id,

                    idpk=self.transfer_idpk,
                    msg_id=self.transfer_msg_id,

                    because_of=str(uuid4()),

                    quantity=Decimal("8820.00"),

                    details={},
                )

            session.rollback()

        self._assert_no_payment_effect(
            expected_budget=Decimal("100000.00"),
            expected_energy=Decimal("50.00"),
        )

    # ============================================================
    # E1-48
    # NO SE PUEDE RECIBIR PAGO DE UNA NEGOCIACIÓN TAKE
    # ============================================================

    def test_received_payment_rejects_take_negotiation(
        self,
    ):

        self._create_cycle_and_negotiation(
            direction="take",
            confirmed_energy=Decimal("40.00"),
            confirmed_price=Decimal("210.00"),
        )

        with Session(engine) as session:

            with self.assertRaises(
                NegotiationPaymentError
            ):
                process_give_payment(
                    session,

                    cycle_id=self.cycle_id,

                    idpk=self.transfer_idpk,
                    msg_id=self.transfer_msg_id,

                    because_of=self.confirmation_msg_id,

                    quantity=Decimal("8400.00"),

                    details={},
                )

            session.rollback()

        self._assert_no_payment_effect(
            expected_budget=Decimal("100000.00"),
            expected_energy=Decimal("50.00"),
        )

    # ============================================================
    # E1-48
    # CICLO INCORRECTO
    # ============================================================

    def test_give_payment_rejects_wrong_cycle(
        self,
    ):

        self._create_cycle_and_negotiation(
            direction="give",
            confirmed_energy=Decimal("40.00"),
            confirmed_price=Decimal("220.50"),
        )

        with Session(engine) as session:

            with self.assertRaises(
                NegotiationPaymentError
            ):
                process_give_payment(
                    session,

                    cycle_id="different-cycle",

                    idpk=self.transfer_idpk,
                    msg_id=self.transfer_msg_id,

                    because_of=self.confirmation_msg_id,

                    quantity=Decimal("8820.00"),

                    details={},
                )

            session.rollback()

        self._assert_no_payment_effect(
            expected_budget=Decimal("100000.00"),
            expected_energy=Decimal("50.00"),
        )

    # ============================================================
    # E1-48
    # NEGOCIACIÓN TODAVÍA NO CONFIRMADA
    # ============================================================

    def test_give_payment_rejects_unconfirmed_negotiation(
        self,
    ):

        self._create_cycle_and_negotiation(
            direction="give",
            status=NEGOTIATION_PROPOSED,
            confirmed_energy=Decimal("40.00"),
            confirmed_price=Decimal("220.50"),
        )

        with Session(engine) as session:

            with self.assertRaises(
                NegotiationPaymentError
            ):
                process_give_payment(
                    session,

                    cycle_id=self.cycle_id,

                    idpk=self.transfer_idpk,
                    msg_id=self.transfer_msg_id,

                    because_of=self.confirmation_msg_id,

                    quantity=Decimal("8820.00"),

                    details={},
                )

            session.rollback()

        self._assert_no_payment_effect(
            expected_budget=Decimal("100000.00"),
            expected_energy=Decimal("50.00"),
        )

    # ============================================================
    # E1-48
    # QUANTITY = 0
    # ============================================================

    def test_give_payment_rejects_zero_quantity(
        self,
    ):

        self._create_cycle_and_negotiation(
            direction="give",
        )

        with Session(engine) as session:

            with self.assertRaises(
                NegotiationPaymentError
            ):
                process_give_payment(
                    session,

                    cycle_id=self.cycle_id,

                    idpk=self.transfer_idpk,
                    msg_id=self.transfer_msg_id,

                    because_of=self.confirmation_msg_id,

                    quantity=Decimal("0.00"),

                    details={},
                )

            session.rollback()

        self._assert_no_payment_effect(
            expected_budget=Decimal("100000.00"),
            expected_energy=Decimal("50.00"),
        )

    # ============================================================
    # E1-48
    # QUANTITY NEGATIVO
    # ============================================================

    def test_give_payment_rejects_negative_quantity(
        self,
    ):

        self._create_cycle_and_negotiation(
            direction="give",
        )

        with Session(engine) as session:

            with self.assertRaises(
                NegotiationPaymentError
            ):
                process_give_payment(
                    session,

                    cycle_id=self.cycle_id,

                    idpk=self.transfer_idpk,
                    msg_id=self.transfer_msg_id,

                    because_of=self.confirmation_msg_id,

                    quantity=Decimal("-10.00"),

                    details={},
                )

            session.rollback()

        self._assert_no_payment_effect(
            expected_budget=Decimal("100000.00"),
            expected_energy=Decimal("50.00"),
        )


if __name__ == "__main__":
    unittest.main()