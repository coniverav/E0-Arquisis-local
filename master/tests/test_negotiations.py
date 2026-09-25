import time
import unittest
from datetime import datetime, timezone
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
from app.models import Cycle, Negotiation


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
                delete(Negotiation).where(
                    Negotiation.cycle_id.in_(self.created_cycle_ids)
                )
            )
            session.exec(
                delete(Cycle).where(Cycle.cycle_id.in_(self.created_cycle_ids))
            )
            session.commit()

    def _create_cycle(self):
        cycle_id = f"cycle-neg-test-{uuid4()}"
        self.created_cycle_ids.append(cycle_id)

        with Session(engine) as session:
            session.add(
                Cycle(
                    cycle_id=cycle_id,
                    opening_budget_balance=Decimal("1000.00"),
                    opening_energy_balance=Decimal("-30.00"),
                    budget_balance=Decimal("1000.00"),
                    energy_balance=Decimal("-30.00"),
                    created_at=datetime.now(timezone.utc),
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

    def test_create_negotiation_returns_201_and_persists_proposed(self):
        cycle_id = self._create_cycle()
        body = self._body(cycle_id)

        response = self.client.post(
            "/negotiations", json=body, headers=self._headers()
        )

        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(data["idpk"], body["idpk"])
        self.assertEqual(data["status"], "PROPOSED")
        self.assertEqual(data["direction"], "give")
        self.assertEqual(Decimal(str(data["requestedQuantity"])), Decimal("10.50"))
        self.assertIsNotNone(data["deadlineAt"])

        with Session(engine) as session:
            stored = session.exec(
                select(Negotiation).where(Negotiation.idpk == body["idpk"])
            ).one()
            self.assertEqual(stored.cycle_id, cycle_id)
            self.assertEqual(stored.status, "PROPOSED")

    def test_duplicate_idpk_returns_200_with_same_negotiation(self):
        cycle_id = self._create_cycle()
        body = self._body(cycle_id)

        first = self.client.post(
            "/negotiations", json=body, headers=self._headers()
        )
        second = self.client.post(
            "/negotiations", json=body, headers=self._headers()
        )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["id"], first.json()["id"])

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
