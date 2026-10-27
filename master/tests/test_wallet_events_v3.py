"""E2-021/E2-022: eventos swap-* por el ingress real, con fixtures oficiales.

El objetivo es que el test no llame directamente a `WalletService`: usa
`POST /internal/messages`, la misma ruta del connector, y verifica
idempotencia, correlación, reserva y snapshot contra el contrato v3.0.1.
"""

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlmodel import Session, select

from app.database import engine, run_migrations
from app.main import app
from app.models import (
    InboundMessage,
    MarketOffer,
    OfferReservation,
    ProcessedIdpk,
    SwapRecord,
    WalletBalanceSnapshot,
)
from app.services.idempotency import is_idpk_processed
from app.services.wallet_service import WalletService
from tests.contracts_v3 import example, validate


class SwapEventIngressTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        run_migrations()
        cls.client = TestClient(app)

    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.cycle_id = f"swap-test-{uuid4()}"
        self.offer_id = f"KLD-{uuid4().hex[:12]}"
        self.swap_id = f"swp-{uuid4().hex[:12]}"
        self.swap_idpk = str(uuid4())
        self.envelope_idpk = str(uuid4())
        self.msg_id = str(uuid4())

        official = deepcopy(example("swap-settled.json"))
        official["idpk"] = self.envelope_idpk
        official["msgId"] = self.msg_id
        official["timestamp"] = self.now.isoformat().replace("+00:00", "Z")
        official["data"]["idpk"] = self.swap_idpk
        official["data"]["swapId"] = self.swap_id
        official["data"]["memo"] = self.offer_id

        validate("amqp/swap-settled", official)
        self.envelope = official

    def tearDown(self):
        with Session(engine) as session:
            session.exec(
                delete(WalletBalanceSnapshot).where(
                    WalletBalanceSnapshot.cycle_id == self.cycle_id
                )
            )
            session.exec(
                delete(OfferReservation).where(
                    OfferReservation.offer_id == self.offer_id
                )
            )
            session.exec(
                delete(MarketOffer).where(
                    MarketOffer.offer_id == self.offer_id
                )
            )
            session.exec(
                delete(SwapRecord).where(SwapRecord.idpk == self.swap_idpk)
            )
            session.exec(
                delete(InboundMessage).where(
                    InboundMessage.idpk == self.envelope_idpk
                )
            )
            session.exec(
                delete(ProcessedIdpk).where(
                    ProcessedIdpk.idpk == self.envelope_idpk
                )
            )
            session.commit()

    def _seed(self, *, with_swap: bool = True, with_offer: bool = True):
        with Session(engine) as session:
            if with_offer:
                session.add(MarketOffer(
                    offer_id=self.offer_id,
                    cycle_id=self.cycle_id,
                    city_id="KLD",
                    is_local=True,
                    initial_quantity=20000,
                    remaining_quantity=20000,
                    price_per_energy=Decimal("330.00"),
                    status="ACTIVE",
                    published_at=self.now,
                    last_updated_at=self.now,
                ))
                session.add(OfferReservation(
                    offer_id=self.offer_id,
                    bid_idpk=self.swap_idpk,
                    quantity=7300,
                    status="HOLD",
                    created_at=self.now,
                ))

            if with_swap:
                WalletService().get_or_create_swap_record(
                    session,
                    idpk=self.swap_idpk,
                    cycle_id=self.cycle_id,
                    role="PROPOSER",
                    counterpart_city_id="COR",
                    give_asset="KWH",
                    give_amount=Decimal("7300"),
                    take_asset="CR",
                    take_amount=Decimal("2409000"),
                    memo=self.offer_id,
                    swap_id=self.swap_id,
                )

            session.commit()

    def _post(self, envelope: dict | None = None):
        return self.client.post(
            "/internal/messages",
            json=envelope or self.envelope,
        )

    def _snapshots(self):
        with Session(engine) as session:
            return session.exec(
                select(WalletBalanceSnapshot).where(
                    WalletBalanceSnapshot.cycle_id == self.cycle_id,
                    WalletBalanceSnapshot.source == "EVENT",
                )
            ).all()

    def test_swap_settled_records_official_balances(self):
        self._seed()

        response = self._post()

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "ok")

        with Session(engine) as session:
            record = session.exec(
                select(SwapRecord).where(SwapRecord.idpk == self.swap_idpk)
            ).one()
            self.assertEqual(record.status, "SETTLED")
            self.assertEqual(record.cycle_id, self.cycle_id)

            reservation = session.exec(
                select(OfferReservation).where(
                    OfferReservation.offer_id == self.offer_id
                )
            ).one()
            self.assertEqual(reservation.status, "COMMITTED")

            offer = session.exec(
                select(MarketOffer).where(MarketOffer.offer_id == self.offer_id)
            ).one()
            self.assertEqual(offer.remaining_quantity, 12700)

        snapshots = self._snapshots()
        self.assertEqual(len(snapshots), 1)

        snapshot = snapshots[0]
        self.assertEqual(snapshot.kwh_available, Decimal("7300"))
        self.assertEqual(snapshot.kwh_held, Decimal("0"))
        self.assertEqual(snapshot.cr_available, Decimal("1791000.0"))
        self.assertEqual(snapshot.cr_held, Decimal("0.0"))
        self.assertIsNone(snapshot.credit_line)

    def test_replay_is_duplicate_and_does_not_duplicate_snapshot(self):
        self._seed()

        self.assertEqual(self._post().json()["status"], "ok")
        replay = self._post()

        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json()["status"], "duplicate")
        self.assertEqual(len(self._snapshots()), 1)

    def test_unknown_swap_is_not_processed_and_releases_idpk(self):
        self._seed(with_swap=False, with_offer=False)

        response = self._post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "not_processed")
        self.assertEqual(response.json()["reason"], "SWAP_UNKNOWN")
        self.assertEqual(len(self._snapshots()), 0)

        with Session(engine) as session:
            self.assertFalse(is_idpk_processed(session, self.envelope_idpk))

        # El evento puede reprocesarse cuando el swap ya existe.
        self._seed()
        retry = self._post()
        self.assertEqual(retry.json()["status"], "ok")
        self.assertEqual(len(self._snapshots()), 1)

    def test_lowercase_balances_after_is_rejected_without_partial_state(self):
        self._seed()
        invalid = deepcopy(self.envelope)
        invalid["data"]["balancesAfter"] = {
            "kwh": {"available": 1, "held": 0},
            "cr": {"available": 1, "held": 0},
            "creditLine": 0,
        }

        response = self._post(invalid)

        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(len(self._snapshots()), 0)

        with Session(engine) as session:
            record = session.exec(
                select(SwapRecord).where(SwapRecord.idpk == self.swap_idpk)
            ).one()
            self.assertEqual(record.status, "SUBMITTED")

            reservation = session.exec(
                select(OfferReservation).where(
                    OfferReservation.offer_id == self.offer_id
                )
            ).one()
            self.assertEqual(reservation.status, "HOLD")
