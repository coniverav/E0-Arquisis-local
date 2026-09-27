import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
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
    Negotiation,
    ProcessedIdpk,
)

from app.services.negotiation_confirmations import (
    NegotiationConfirmationError,
    process_negotiation_confirmation,
)

from app.services.negotiation_state import (
    NEGOTIATION_ACKNOWLEDGED,
    NEGOTIATION_CONFIRMED,
    NEGOTIATION_PENDING_PUBLICATION,
    NEGOTIATION_PROPOSED,
    InvalidNegotiationTransition,
)


class NegotiationConfirmationTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        """
        Ejecuta las migraciones antes de comenzar los tests.
        """
        run_migrations()
        cls.client = TestClient(app)

    def setUp(self):
        """
        Cada test utiliza identificadores únicos para no interferir
        con otros tests ni con datos existentes.
        """

        self.cycle_id = (
            f"confirmation-test-{uuid4()}"
        )

        self.proposal_idpk = str(uuid4())
        self.proposal_msg_id = str(uuid4())

        self.confirmation_idpk = str(uuid4())
        self.confirmation_msg_id = str(uuid4())

        self.retry_msg_id = str(uuid4())

    def tearDown(self):
        """
        Elimina toda la información creada por el test.

        El orden importa por las foreign keys:
        LedgerEntry -> Negotiation -> Cycle.
        """

        with Session(engine) as session:

            session.exec(
                delete(InboundMessage).where(
                    InboundMessage.cycle_id
                    == self.cycle_id
                )
            )

            session.exec(
                delete(ProcessedIdpk).where(
                    ProcessedIdpk.cycle_id
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
        status: str,
        energy_balance: Decimal,
        requested_quantity: Decimal = Decimal("50.00"),
    ) -> None:
        """
        Crea un ciclo y una negociación ya existente.

        status_idpk se deja definido para evitar que
        /internal/messages intente generar automáticamente
        un request de status-statement durante estos tests.
        """

        now = datetime.now(timezone.utc)

        with Session(engine) as session:

            cycle = Cycle(
                cycle_id=self.cycle_id,

                # Simulamos que ya llegó el status-statement.
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

                opening_budget_balance=Decimal("1000.00"),
                opening_energy_balance=energy_balance,

                budget_balance=Decimal("1000.00"),
                energy_balance=energy_balance,

                last_sequence=0,

                created_at=now,
            )

            session.add(cycle)
            session.flush()

            negotiation = Negotiation(
                cycle_id=self.cycle_id,

                # idpk original de negotiation-proposal.
                idpk=self.proposal_idpk,

                # El give/take debe apuntar a este msgId.
                latest_msg_id=self.proposal_msg_id,

                direction=direction,

                requested_quantity=requested_quantity,

                offered_price=(
                    Decimal("220.50")
                    if direction == "give"
                    else Decimal("210.00")
                ),

                status=status,

                deadline_at=(
                    now + timedelta(seconds=30)
                ),

                created_at=now,
                updated_at=now,
            )

            session.add(negotiation)
            session.commit()

    def _confirmation_payload(
        self,
        *,
        message_type: str,
        idpk: str | None = None,
        msg_id: str | None = None,
        target: str | None = None,
        energy: float = 40,
        price: float | None = None,
    ) -> dict:

        if idpk is None:
            idpk = self.confirmation_idpk

        if msg_id is None:
            msg_id = self.confirmation_msg_id

        if target is None:
            target = self.proposal_msg_id

        if price is None:
            price = (
                220.5
                if message_type == "give"
                else 210
            )

        return {
            "idpk": idpk,
            "msgId": msg_id,
            "type": message_type,
            "timestamp": datetime.now(
                timezone.utc
            ).isoformat(),
            "cycleId": self.cycle_id,
            "sender": "central",
            "data": {
                "target": target,
                "energy": energy,
                "pricePerEnergy": price,
            },
        }

    # ============================================================
    # E1-45
    # GIVE VÁLIDO
    # ============================================================

    def test_give_confirmation_is_applied_and_associated(self):

        self._create_cycle_and_negotiation(
            direction="give",
            status=NEGOTIATION_ACKNOWLEDGED,
            energy_balance=Decimal("80.00"),
        )

        payload = self._confirmation_payload(
            message_type="give",
            energy=40,
            price=220.5,
        )

        response = self.client.post(
            "/internal/messages",
            json=payload,
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

            negotiation = session.exec(
                select(Negotiation).where(
                    Negotiation.cycle_id
                    == self.cycle_id
                )
            ).one()

            entries = session.exec(
                select(LedgerEntry).where(
                    LedgerEntry.cycle_id
                    == self.cycle_id
                )
            ).all()

            # ----------------------------------------
            # Ledger
            # ----------------------------------------

            self.assertEqual(
                len(entries),
                1,
            )

            entry = entries[0]

            self.assertEqual(
                entry.operation_type,
                "GIVE_CONFIRMED",
            )

            # GIVE = entregamos energía.
            self.assertEqual(
                entry.energy_delta,
                Decimal("-40.00"),
            )

            # La confirmación todavía no mueve dinero.
            self.assertEqual(
                entry.budget_delta,
                Decimal("0.00"),
            )

            self.assertEqual(
                cycle.energy_balance,
                Decimal("40.00"),
            )

            self.assertEqual(
                cycle.budget_balance,
                Decimal("1000.00"),
            )

            # ----------------------------------------
            # Asociación Ledger -> Negotiation
            # ----------------------------------------

            self.assertEqual(
                entry.negotiation_id,
                negotiation.id,
            )

            self.assertEqual(
                entry.source_msg_id,
                self.confirmation_msg_id,
            )

            # ----------------------------------------
            # Negotiation
            # ----------------------------------------

            self.assertEqual(
                negotiation.status,
                NEGOTIATION_CONFIRMED,
            )

            self.assertEqual(
                negotiation.confirmed_energy,
                Decimal("40.00"),
            )

            self.assertEqual(
                negotiation.confirmed_price,
                Decimal("220.50"),
            )

            # Ahora latest_msg_id debe ser el msgId
            # del give, porque el futuro transfer
            # usará becauseOf apuntando a él.
            self.assertEqual(
                negotiation.latest_msg_id,
                self.confirmation_msg_id,
            )

    # ============================================================
    # E1-46
    # TAKE VÁLIDO
    # ============================================================

    def test_take_confirmation_is_applied_and_associated(self):

        # Dejamos status PROPOSED deliberadamente:
        # E1-44 debe permitir PROPOSED -> CONFIRMED.
        self._create_cycle_and_negotiation(
            direction="take",
            status=NEGOTIATION_PROPOSED,
            energy_balance=Decimal("-30.00"),
        )

        payload = self._confirmation_payload(
            message_type="take",
            energy=50,
            price=210,
        )

        response = self.client.post(
            "/internal/messages",
            json=payload,
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

            negotiation = session.exec(
                select(Negotiation).where(
                    Negotiation.cycle_id
                    == self.cycle_id
                )
            ).one()

            # ====================================================
            # E1-46:
            # debe existir exactamente una confirmación TAKE.
            # ====================================================

            confirmation_entries = session.exec(
                select(LedgerEntry).where(
                    LedgerEntry.cycle_id
                    == self.cycle_id,
                    LedgerEntry.operation_type
                    == "TAKE_CONFIRMED",
                )
            ).all()

            self.assertEqual(
                len(confirmation_entries),
                1,
            )

            confirmation_entry = (
                confirmation_entries[0]
            )

            # ====================================================
            # E1-47:
            # después de TAKE_CONFIRMED se crea automáticamente
            # exactamente un PAYMENT_SENT.
            # ====================================================

            payment_entries = session.exec(
                select(LedgerEntry).where(
                    LedgerEntry.cycle_id
                    == self.cycle_id,
                    LedgerEntry.operation_type
                    == "PAYMENT_SENT",
                )
            ).all()

            self.assertEqual(
                len(payment_entries),
                1,
            )

            payment_entry = (
                payment_entries[0]
            )

            # ====================================================
            # Verificaciones de E1-46
            # ====================================================

            # Balance inicial:
            # -30
            #
            # TAKE confirmado:
            # +50
            #
            # Resultado:
            # 20
            self.assertEqual(
                cycle.energy_balance,
                Decimal("20.00"),
            )

            self.assertEqual(
                confirmation_entry.energy_delta,
                Decimal("50.00"),
            )

            self.assertEqual(
                confirmation_entry.budget_delta,
                Decimal("0.00"),
            )

            self.assertEqual(
                confirmation_entry.negotiation_id,
                negotiation.id,
            )

            self.assertEqual(
                negotiation.confirmed_energy,
                Decimal("50.00"),
            )

            self.assertEqual(
                negotiation.confirmed_price,
                Decimal("210.00"),
            )

            self.assertEqual(
                negotiation.status,
                NEGOTIATION_CONFIRMED,
            )

            # ====================================================
            # Verificaciones de E1-47
            # ====================================================

            # El pago no vuelve a modificar energía.
            self.assertEqual(
                payment_entry.energy_delta,
                Decimal("0.00"),
            )

            # 50 * 210 = 10500
            self.assertEqual(
                payment_entry.budget_delta,
                Decimal("-10500.00"),
            )

            self.assertEqual(
                payment_entry.negotiation_id,
                negotiation.id,
            )

            self.assertEqual(
                negotiation.payment_quantity,
                Decimal("10500.00"),
            )

            # ====================================================
            # Ahora hay DOS operaciones en ledger:
            #
            # sequence 1 -> TAKE_CONFIRMED
            # sequence 2 -> PAYMENT_SENT
            # ====================================================

            self.assertEqual(
                cycle.last_sequence,
                2,
            )

            # E1-47 cambia latest_msg_id al msgId del transfer.
            # Debe coincidir con la entrada PAYMENT_SENT.
            self.assertEqual(
                negotiation.latest_msg_id,
                payment_entry.source_msg_id,
            )

            # Ya no debe apuntar al msgId del TAKE recibido.
            self.assertNotEqual(
                negotiation.latest_msg_id,
                str(payload["msgId"]),
            )

    # ============================================================
    # IDEMPOTENCIA GIVE
    # ============================================================

    def test_duplicate_give_is_not_applied_twice(self):

        self._create_cycle_and_negotiation(
            direction="give",
            status=NEGOTIATION_ACKNOWLEDGED,
            energy_balance=Decimal("80.00"),
        )

        first_payload = self._confirmation_payload(
            message_type="give",
            idpk=self.confirmation_idpk,
            msg_id=self.confirmation_msg_id,
            energy=40,
        )

        first_response = self.client.post(
            "/internal/messages",
            json=first_payload,
        )

        self.assertEqual(
            first_response.status_code,
            200,
        )

        self.assertEqual(
            first_response.json()["status"],
            "ok",
        )

        # Retry lógico:
        # mismo idpk, pero msgId nuevo.
        retry_payload = self._confirmation_payload(
            message_type="give",
            idpk=self.confirmation_idpk,
            msg_id=self.retry_msg_id,
            energy=999,
        )

        retry_response = self.client.post(
            "/internal/messages",
            json=retry_payload,
        )

        self.assertEqual(
            retry_response.status_code,
            200,
        )

        self.assertEqual(
            retry_response.json()["status"],
            "duplicate",
        )

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
                    == "GIVE_CONFIRMED",
                )
            ).all()

            negotiation = session.exec(
                select(Negotiation).where(
                    Negotiation.cycle_id
                    == self.cycle_id
                )
            ).one()

            # Solo se aplicó una vez.
            self.assertEqual(
                len(entries),
                1,
            )

            self.assertEqual(
                cycle.energy_balance,
                Decimal("40.00"),
            )

            self.assertEqual(
                cycle.last_sequence,
                1,
            )

            # Debe conservar los datos de la
            # primera confirmación válida.
            self.assertEqual(
                negotiation.confirmed_energy,
                Decimal("40.00"),
            )

            self.assertEqual(
                negotiation.latest_msg_id,
                self.confirmation_msg_id,
            )

    # ============================================================
    # IDEMPOTENCIA TAKE
    # ============================================================

    def test_duplicate_take_is_not_applied_twice(self):

        self._create_cycle_and_negotiation(
            direction="take",
            status=NEGOTIATION_ACKNOWLEDGED,
            energy_balance=Decimal("-30.00"),
        )

        # ========================================================
        # PRIMER TAKE
        # ========================================================

        first_payload = self._confirmation_payload(
            message_type="take",
            idpk=self.confirmation_idpk,
            msg_id=self.confirmation_msg_id,
            energy=50,
            price=210,
        )

        first_response = self.client.post(
            "/internal/messages",
            json=first_payload,
        )

        self.assertEqual(
            first_response.status_code,
            200,
        )

        self.assertEqual(
            first_response.json()["status"],
            "ok",
        )

        # ========================================================
        # SEGUNDO TAKE
        #
        # Mismo idpk:
        # debe ser reconocido como duplicado aunque cambien
        # msgId y energía.
        # ========================================================

        retry_payload = self._confirmation_payload(
            message_type="take",
            idpk=self.confirmation_idpk,
            msg_id=self.retry_msg_id,

            # Valor deliberadamente distinto.
            # Si el duplicado se procesara por error,
            # destruiría el balance y el test fallaría.
            energy=999,

            price=210,
        )

        retry_response = self.client.post(
            "/internal/messages",
            json=retry_payload,
        )

        self.assertEqual(
            retry_response.status_code,
            200,
        )

        self.assertEqual(
            retry_response.json()["status"],
            "duplicate",
        )

        with Session(engine) as session:

            cycle = session.get(
                Cycle,
                self.cycle_id,
            )

            negotiation = session.exec(
                select(Negotiation).where(
                    Negotiation.cycle_id
                    == self.cycle_id
                )
            ).one()

            # ====================================================
            # Confirmación TAKE:
            # debe existir UNA sola vez.
            # ====================================================

            confirmation_entries = session.exec(
                select(LedgerEntry).where(
                    LedgerEntry.cycle_id
                    == self.cycle_id,
                    LedgerEntry.operation_type
                    == "TAKE_CONFIRMED",
                )
            ).all()

            self.assertEqual(
                len(confirmation_entries),
                1,
            )

            confirmation_entry = (
                confirmation_entries[0]
            )

            # ====================================================
            # Pago E1-47:
            # también debe existir UNA sola vez.
            # ====================================================

            payment_entries = session.exec(
                select(LedgerEntry).where(
                    LedgerEntry.cycle_id
                    == self.cycle_id,
                    LedgerEntry.operation_type
                    == "PAYMENT_SENT",
                )
            ).all()

            self.assertEqual(
                len(payment_entries),
                1,
            )

            payment_entry = (
                payment_entries[0]
            )

            # ====================================================
            # El retry con energy=999 NO debe modificar energía.
            #
            # -30 + 50 = 20
            # ====================================================

            self.assertEqual(
                cycle.energy_balance,
                Decimal("20.00"),
            )

            self.assertEqual(
                confirmation_entry.energy_delta,
                Decimal("50.00"),
            )

            # No debe existir TAKE_CONFIRMED con 999.
            self.assertNotEqual(
                confirmation_entry.energy_delta,
                Decimal("999.00"),
            )

            # ====================================================
            # El pago tampoco puede duplicarse.
            #
            # 50 * 210 = 10500
            # ====================================================

            self.assertEqual(
                payment_entry.budget_delta,
                Decimal("-10500.00"),
            )

            self.assertEqual(
                payment_entry.energy_delta,
                Decimal("0.00"),
            )

            # ====================================================
            # Ambas operaciones deben apuntar a la misma
            # negociación.
            # ====================================================

            self.assertEqual(
                confirmation_entry.negotiation_id,
                negotiation.id,
            )

            self.assertEqual(
                payment_entry.negotiation_id,
                negotiation.id,
            )

            # ====================================================
            # Negotiation conserva SOLO los valores de la
            # primera confirmación.
            # ====================================================

            self.assertEqual(
                negotiation.confirmed_energy,
                Decimal("50.00"),
            )

            self.assertEqual(
                negotiation.confirmed_price,
                Decimal("210.00"),
            )

            self.assertEqual(
                negotiation.payment_quantity,
                Decimal("10500.00"),
            )

            self.assertEqual(
                negotiation.status,
                NEGOTIATION_CONFIRMED,
            )

            # ====================================================
            # Exactamente DOS efectos reales:
            #
            # 1 -> TAKE_CONFIRMED
            # 2 -> PAYMENT_SENT
            #
            # El retry no crea sequence 3.
            # ====================================================

            self.assertEqual(
                cycle.last_sequence,
                2,
            )

            # latest_msg_id debe seguir correspondiendo al
            # transfer generado por el PRIMER TAKE.
            self.assertEqual(
                negotiation.latest_msg_id,
                payment_entry.source_msg_id,
            )

            # El msgId del retry nunca debe convertirse
            # en latest_msg_id.
            self.assertNotEqual(
                negotiation.latest_msg_id,
                self.retry_msg_id,
            )

    # ============================================================
    # DIRECCIÓN INCORRECTA
    # ============================================================

    def test_give_cannot_confirm_take_negotiation(self):

        self._create_cycle_and_negotiation(
            direction="take",
            status=NEGOTIATION_ACKNOWLEDGED,
            energy_balance=Decimal("-30.00"),
        )

        with Session(engine) as session:

            with self.assertRaises(
                NegotiationConfirmationError
            ):
                process_negotiation_confirmation(
                    session,
                    confirmation_type="give",
                    cycle_id=self.cycle_id,
                    idpk=self.confirmation_idpk,
                    msg_id=self.confirmation_msg_id,
                    target_msg_id=self.proposal_msg_id,
                    energy=Decimal("40.00"),
                    price_per_energy=Decimal("220.50"),
                    details={},
                )

            session.rollback()

        self._assert_no_ledger_effect(
            expected_energy=Decimal("-30.00")
        )

    # ============================================================
    # CICLO INCORRECTO
    # ============================================================

    def test_confirmation_with_wrong_cycle_is_rejected(self):

        self._create_cycle_and_negotiation(
            direction="give",
            status=NEGOTIATION_ACKNOWLEDGED,
            energy_balance=Decimal("80.00"),
        )

        with Session(engine) as session:

            with self.assertRaises(
                NegotiationConfirmationError
            ):
                process_negotiation_confirmation(
                    session,
                    confirmation_type="give",

                    # No corresponde al ciclo de la negociación.
                    cycle_id="another-cycle",

                    idpk=self.confirmation_idpk,
                    msg_id=self.confirmation_msg_id,
                    target_msg_id=self.proposal_msg_id,
                    energy=Decimal("40.00"),
                    price_per_energy=Decimal("220.50"),
                    details={},
                )

            session.rollback()

        self._assert_no_ledger_effect(
            expected_energy=Decimal("80.00")
        )

    # ============================================================
    # ENERGÍA INVÁLIDA
    # ============================================================

    def test_zero_energy_is_rejected(self):

        self._create_cycle_and_negotiation(
            direction="give",
            status=NEGOTIATION_ACKNOWLEDGED,
            energy_balance=Decimal("80.00"),
        )

        with Session(engine) as session:

            with self.assertRaises(
                NegotiationConfirmationError
            ):
                process_negotiation_confirmation(
                    session,
                    confirmation_type="give",
                    cycle_id=self.cycle_id,
                    idpk=self.confirmation_idpk,
                    msg_id=self.confirmation_msg_id,
                    target_msg_id=self.proposal_msg_id,
                    energy=Decimal("0.00"),
                    price_per_energy=Decimal("220.50"),
                    details={},
                )

            session.rollback()

        self._assert_no_ledger_effect(
            expected_energy=Decimal("80.00")
        )

    # ============================================================
    # PRECIO INVÁLIDO
    # ============================================================

    def test_negative_price_is_rejected(self):

        self._create_cycle_and_negotiation(
            direction="take",
            status=NEGOTIATION_ACKNOWLEDGED,
            energy_balance=Decimal("-30.00"),
        )

        with Session(engine) as session:

            with self.assertRaises(
                NegotiationConfirmationError
            ):
                process_negotiation_confirmation(
                    session,
                    confirmation_type="take",
                    cycle_id=self.cycle_id,
                    idpk=self.confirmation_idpk,
                    msg_id=self.confirmation_msg_id,
                    target_msg_id=self.proposal_msg_id,
                    energy=Decimal("50.00"),
                    price_per_energy=Decimal("-1.00"),
                    details={},
                )

            session.rollback()

        self._assert_no_ledger_effect(
            expected_energy=Decimal("-30.00")
        )

    # ============================================================
    # ESTADO INVÁLIDO
    # ============================================================

    def test_confirmation_from_pending_publication_is_rejected_and_rolled_back(
        self,
    ):

        self._create_cycle_and_negotiation(
            direction="give",
            status=NEGOTIATION_PENDING_PUBLICATION,
            energy_balance=Decimal("80.00"),
        )

        with Session(engine) as session:

            with self.assertRaises(
                InvalidNegotiationTransition
            ):
                process_negotiation_confirmation(
                    session,
                    confirmation_type="give",
                    cycle_id=self.cycle_id,
                    idpk=self.confirmation_idpk,
                    msg_id=self.confirmation_msg_id,
                    target_msg_id=self.proposal_msg_id,
                    energy=Decimal("40.00"),
                    price_per_energy=Decimal("220.50"),
                    details={},
                )

            # MUY IMPORTANTE:
            # si apply_ledger_effect alcanzó a hacer flush()
            # antes de descubrir la transición inválida,
            # el rollback debe eliminar ese efecto.
            session.rollback()

        self._assert_no_ledger_effect(
            expected_energy=Decimal("80.00")
        )

    # ============================================================
    # HELPER PARA COMPROBAR QUE NO HUBO EFECTOS
    # ============================================================

    def _assert_no_ledger_effect(
        self,
        *,
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
                    == self.cycle_id
                )
            ).all()

            negotiation = session.exec(
                select(Negotiation).where(
                    Negotiation.cycle_id
                    == self.cycle_id
                )
            ).one()

            self.assertEqual(
                len(entries),
                0,
            )

            self.assertEqual(
                cycle.energy_balance,
                expected_energy,
            )

            self.assertEqual(
                cycle.budget_balance,
                Decimal("1000.00"),
            )

            self.assertEqual(
                cycle.last_sequence,
                0,
            )

            self.assertNotEqual(
                negotiation.status,
                NEGOTIATION_CONFIRMED,
            )


if __name__ == "__main__":
    unittest.main()