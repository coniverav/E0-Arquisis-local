import unittest
from datetime import datetime, timezone
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
    ProcessedIdpk,
)
from app.services.idempotency import (
    claim_idpk,
    is_idpk_processed,
)


class IdempotencyTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        run_migrations()
        cls.client = TestClient(app)

    def setUp(self):
        self.cycle_id = f"idempotency-test-{uuid4()}"
        self.idpk = str(uuid4())
        self.first_msg_id = str(uuid4())
        self.retry_msg_id = str(uuid4())

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
                    ProcessedIdpk.idpk == self.idpk
                )
            )

            cycle = session.get(Cycle, self.cycle_id)

            if cycle is not None:
                session.delete(cycle)

            session.commit()

    def test_processed_idpk_can_be_checked_deterministically(self):
        #Un idpk nuevo no debe aparecer inicialmente como procesado.
        with Session(engine) as session:
            self.assertFalse(
                is_idpk_processed(
                    session,
                    self.idpk,
                )
            )

            #El primer claim registra el idpk dentro de la transacción.
            claimed = claim_idpk(
                session,
                idpk=self.idpk,
                msg_id=self.first_msg_id,
                message_type="transfer",
                cycle_id=self.cycle_id,
            )

            self.assertTrue(claimed)

            session.commit()

        #Una sesión distinta debe observar el claim confirmado.
        #Un segundo intento con el mismo idpk debe ser rechazado.
        with Session(engine) as session:
            self.assertTrue(
                is_idpk_processed(
                    session,
                    self.idpk,
                )
            )

            claimed_again = claim_idpk(
                session,
                idpk=self.idpk,
                msg_id=self.retry_msg_id,
                message_type="transfer",
                cycle_id=self.cycle_id,
            )

            self.assertFalse(claimed_again)

    def test_same_idpk_cannot_apply_different_transfer_effect_twice(self):
        #Primera entrega, un transfer normal se interpreta como ingreso de presupuesto (TRANSFER_IN).
        first_payload = {
            "idpk": self.idpk,
            "msgId": self.first_msg_id,
            "type": "transfer",
            "timestamp": datetime.now(
                timezone.utc
            ).isoformat(),
            "cycleId": self.cycle_id,
            "sender": "central",
            "data": {
                "quantity": 120.50,
            },
        }

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
        
        #Retry adversarial, se conserva el mismo idpk, pero cambia el
        #payload agregando becauseOf. Sin idempotencia global, esto
        #podría derivar en otro operation_type y aplicar un segundo efecto.
        retry_payload = {
            "idpk": self.idpk,
            "msgId": self.retry_msg_id,
            "type": "transfer",
            "timestamp": datetime.now(
                timezone.utc
            ).isoformat(),
            "cycleId": self.cycle_id,
            "sender": "central",
            "data": {
                "quantity": 999.99,
                "becauseOf": str(uuid4()),
            },
        }

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

        #El segundo mensaje debe quedar auditado, pero sin generar un nuevo efecto en el ledger ni modificar los balances.
        with Session(engine) as session:
            cycle = session.get(
                Cycle,
                self.cycle_id,
            )

            self.assertIsNotNone(cycle)

            self.assertEqual(
                cycle.budget_balance,
                Decimal("120.50"),
            )
            self.assertEqual(
                cycle.energy_balance,
                Decimal("0.00"),
            )
            self.assertEqual(
                cycle.last_sequence,
                1,
            )

            entries = session.exec(
                select(LedgerEntry).where(
                    LedgerEntry.cycle_id
                    == self.cycle_id
                )
            ).all()

            #Solo debe existir el efecto producido por el primer mensaje.
            self.assertEqual(len(entries), 1)
            self.assertEqual(
                entries[0].operation_type,
                "TRANSFER_IN",
            )
            self.assertEqual(
                entries[0].idpk,
                self.idpk,
            )
            self.assertEqual(
                entries[0].source_msg_id,
                self.first_msg_id,
            )

            processed = session.exec(
                select(ProcessedIdpk).where(
                    ProcessedIdpk.idpk
                    == self.idpk
                )
            ).all()

            #El registro global conserva el mensaje que obtuvo originalmente el claim del idpk.
            self.assertEqual(len(processed), 1)
            self.assertEqual(
                processed[0].msg_id,
                self.first_msg_id,
            )
            self.assertEqual(
                processed[0].message_type,
                "transfer",
            )

            inbound = session.exec(
                select(InboundMessage)
                .where(
                    InboundMessage.cycle_id
                    == self.cycle_id
                )
                .order_by(InboundMessage.id)
            ).all()

            #Ambas recepciones quedan auditadas aunque solo una haya producido efectos de negocio.
            self.assertEqual(len(inbound), 2)
            self.assertEqual(
                inbound[0].status,
                "PROCESSED",
            )
            self.assertEqual(
                inbound[1].status,
                "DUPLICATE",
            )
            self.assertEqual(
                inbound[1].reason,
                "idpk already processed",
            )

    def test_rolled_back_claim_can_be_retried(self):
        idpk = str(uuid4())
        first_msg_id = str(uuid4())
        retry_msg_id = str(uuid4())

        #El claim forma parte de la misma transacción que el efecto.
        #Si la operación falla, un rollback también debe revertirlo.
        with Session(engine) as session:
            claimed = claim_idpk(
                session,
                idpk=idpk,
                msg_id=first_msg_id,
                message_type="transfer",
                cycle_id=self.cycle_id,
            )

            self.assertTrue(claimed)

            session.rollback()

        #Después del rollback, el idpk no debe quedar marcado como procesado y debe ser posible reclamarlo nuevamente.
        with Session(engine) as session:
            self.assertFalse(
                is_idpk_processed(
                    session,
                    idpk,
                )
            )

            claimed_again = claim_idpk(
                session,
                idpk=idpk,
                msg_id=retry_msg_id,
                message_type="transfer",
                cycle_id=self.cycle_id,
            )

            self.assertTrue(claimed_again)

            session.rollback()

if __name__ == "__main__":
    unittest.main()