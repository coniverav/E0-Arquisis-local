import time
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest import mock
from uuid import uuid4

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlmodel import Session, select

from app import auth
from app.database import engine, run_migrations
from app.main import app
from app.models import Cycle, Negotiation, OutboundMessage
from app.config import CITY_ID, RABBITMQ_CENTRAL_ROUTING_KEY


class NegotiationCreateTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        run_migrations()
        cls.client = TestClient(app)

        # RSA local para firmar tokens; el JWKS se mockea.
        cls.kid = "test-key"
        cls.issuer = "https://test-issuer/"
        cls.audience = "test-audience"

        private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )
        jwk = jwt.algorithms.RSAAlgorithm.to_jwk(
            private_key.public_key(),
            as_dict=True,
        )
        jwk.update({"kid": cls.kid, "alg": "RS256", "use": "sig"})
        cls.jwks = {"keys": [jwk]}
        cls.private_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )

        cls.patchers = [
            mock.patch.object(
                auth, "AUTH_JWKS_URL", "https://test-issuer/.well-known/jwks.json"
            ),
            mock.patch.object(auth, "AUTH_ISSUER", cls.issuer),
            mock.patch.object(auth, "AUTH_AUDIENCE", cls.audience),
            mock.patch.object(auth, "fetch_jwks", lambda url: cls.jwks),
        ]
        for patcher in cls.patchers:
            patcher.start()

    @classmethod
    def tearDownClass(cls):
        for patcher in cls.patchers:
            patcher.stop()

    def setUp(self):
        self.created_cycle_ids = []

    def tearDown(self):
        if not self.created_cycle_ids:
            return

        with Session(engine) as session:
            session.exec(
                delete(OutboundMessage).where(
                    OutboundMessage.cycle_id.in_(self.created_cycle_ids)
                )
            )
            session.exec(
                delete(Negotiation).where(
                    Negotiation.cycle_id.in_(self.created_cycle_ids)
                )
            )
            session.exec(
                delete(Cycle).where(
                    Cycle.cycle_id.in_(self.created_cycle_ids)
                )
            )
            session.commit()

    def _create_cycle(self):
        cycle_id = f"cycle-neg-test-{uuid4()}"
        self.created_cycle_ids.append(cycle_id)

        now = datetime.now(timezone.utc)

        with Session(engine) as session:
            session.add(
                Cycle(
                    cycle_id=cycle_id,
                    scheduler_state="NEGOTIATING",
                    generation_capacity=Decimal("150.00"),
                    consumption=Decimal("100.00"),
                    generation_cost=Decimal("4.00"),
                    valid_until=now + timedelta(minutes=10),
                    opening_budget_balance=Decimal("1000.00"),
                    opening_energy_balance=Decimal("-30.00"),
                    budget_balance=Decimal("1000.00"),
                    energy_balance=Decimal("-30.00"),
                    status_payload={},
                    created_at=now,
                )
            )
            session.commit()

        return cycle_id

    def _token(self, lifetime_seconds=300, key_pem=None):
        now = int(time.time())
        return jwt.encode(
            {
                "sub": "test-user",
                "iss": self.issuer,
                "aud": self.audience,
                "iat": now,
                "exp": now + lifetime_seconds,
            },
            key_pem or self.private_pem,
            algorithm="RS256",
            headers={"kid": self.kid},
        )

    def _headers(self, token=None):
        return {"Authorization": f"Bearer {token or self._token()}"}

    def _body(self, cycle_id, **overrides):
        body = {
            "cycleId": cycle_id,
            "idpk": str(uuid4()),
            "direction": "give",
            "requestedQuantity": "10.50",
            "offeredPrice": "3.25",
        }
        body.update(overrides)
        return body

    def test_create_negotiation_returns_201_and_enqueues_proposal(self):
        cycle_id = self._create_cycle()
        body = self._body(cycle_id)

        response = self.client.post(
            "/negotiations",
            json=body,
            headers=self._headers(),
        )

        self.assertEqual(response.status_code, 201)

        data = response.json()

        self.assertEqual(data["idpk"], body["idpk"])
        self.assertEqual(
            data["status"],
            "PENDING_PUBLICATION",
        )
        self.assertEqual(data["direction"], "give")
        self.assertEqual(
            Decimal(str(data["requestedQuantity"])),
            Decimal("10.50"),
        )

        #Los 30 segundos empiezan cuando RabbitMQ confirma la publicación, no al crearla.
        self.assertIsNone(data["deadlineAt"])

        with Session(engine) as session:
            stored = session.exec(
                select(Negotiation).where(
                    Negotiation.idpk == body["idpk"]
                )
            ).one()

            self.assertEqual(
                stored.cycle_id,
                cycle_id,
            )
            self.assertEqual(
                stored.status,
                "PENDING_PUBLICATION",
            )
            self.assertIsNone(stored.deadline_at)
            self.assertIsNotNone(stored.latest_msg_id)

            outbound = session.exec(
                select(OutboundMessage).where(
                    OutboundMessage.msg_id
                    == stored.latest_msg_id
                )
            ).one()

            self.assertEqual(
                outbound.idpk,
                body["idpk"],
            )
            self.assertEqual(
                outbound.message_type,
                "negotiation-proposal",
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
                RABBITMQ_CENTRAL_ROUTING_KEY,
            )

            self.assertNotEqual(
                outbound.msg_id,
                outbound.idpk,
            )

            self.assertEqual(
                outbound.payload["idpk"],
                body["idpk"],
            )

            self.assertEqual(
                outbound.payload["msgId"],
                outbound.msg_id,
            )

            self.assertEqual(
                outbound.payload["cityId"],
                CITY_ID,
            )

            self.assertEqual(
                outbound.payload["cycleId"],
                cycle_id,
            )
            self.assertEqual(
                outbound.payload["type"],
                "negotiation-proposal",
            )
            self.assertEqual(
                outbound.payload["data"]["direction"],
                "give",
            )
            self.assertEqual(
                Decimal(
                    str(
                        outbound.payload["data"]["quantity"]
                    )
                ),
                Decimal("10.50"),
            )
            self.assertEqual(
                Decimal(
                    str(
                        outbound.payload["data"][
                            "pricePerEnergy"
                        ]
                    )
                ),
                Decimal("3.25"),
            )

    def test_duplicate_idpk_returns_200_without_duplicate_outbound(self):
        cycle_id = self._create_cycle()
        body = self._body(cycle_id)

        first = self.client.post(
            "/negotiations",
            json=body,
            headers=self._headers(),
        )

        second = self.client.post(
            "/negotiations",
            json=body,
            headers=self._headers(),
        )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)

        self.assertEqual(
            second.json()["id"],
            first.json()["id"],
        )
        self.assertEqual(
            second.json()["idpk"],
            body["idpk"],
        )

        with Session(engine) as session:
            negotiations = session.exec(
                select(Negotiation).where(
                    Negotiation.idpk == body["idpk"]
                )
            ).all()

            outbounds = session.exec(
                select(OutboundMessage).where(
                    OutboundMessage.idpk == body["idpk"],
                    OutboundMessage.message_type
                    == "negotiation-proposal",
                )
            ).all()

            self.assertEqual(len(negotiations), 1)
            self.assertEqual(len(outbounds), 1)

    def test_unknown_cycle_returns_404(self):
        response = self.client.post(
            "/negotiations",
            json=self._body("cycle-inexistente"),
            headers=self._headers(),
        )

        self.assertEqual(response.status_code, 404)

    def test_missing_token_returns_401(self):
        cycle_id = self._create_cycle()

        response = self.client.post("/negotiations", json=self._body(cycle_id))

        self.assertEqual(response.status_code, 401)

    def test_tampered_token_returns_401(self):
        cycle_id = self._create_cycle()

        response = self.client.post(
            "/negotiations",
            json=self._body(cycle_id),
            headers=self._headers(token=self._token() + "X"),
        )

        self.assertEqual(response.status_code, 401)

    def test_expired_token_returns_401(self):
        cycle_id = self._create_cycle()

        response = self.client.post(
            "/negotiations",
            json=self._body(cycle_id),
            headers=self._headers(token=self._token(lifetime_seconds=-10)),
        )

        self.assertEqual(response.status_code, 401)

    def test_invalid_direction_returns_422(self):
        cycle_id = self._create_cycle()

        response = self.client.post(
            "/negotiations",
            json=self._body(cycle_id, direction="swap"),
            headers=self._headers(),
        )

        self.assertEqual(response.status_code, 422)

    def test_non_positive_quantity_returns_422(self):
        cycle_id = self._create_cycle()

        response = self.client.post(
            "/negotiations",
            json=self._body(cycle_id, requestedQuantity="0"),
            headers=self._headers(),
        )

        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
