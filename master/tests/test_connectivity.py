import unittest
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlmodel import Session

from app.database import engine, run_migrations
from app.main import app
from app.models import (
    DistanceTable,
    InboundMessage,
    ProcessedIdpk,
)


class ConnectivityTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        run_migrations()
        cls.client = TestClient(app)

    def setUp(self):
        self.created_idpks = []

    def tearDown(self):
        if not self.created_idpks:
            return

        with Session(engine) as session:
            session.exec(
                delete(InboundMessage).where(
                    InboundMessage.idpk.in_(
                        self.created_idpks
                    )
                )
            )

            session.exec(
                delete(DistanceTable).where(
                    DistanceTable.idpk.in_(
                        self.created_idpks
                    )
                )
            )

            session.exec(
                delete(ProcessedIdpk).where(
                    ProcessedIdpk.idpk.in_(
                        self.created_idpks
                    )
                )
            )

            session.commit()

    def _publish_distance_table(
        self,
        *,
        timestamp: str,
        distances: dict,
    ):
        idpk = str(uuid4())
        self.created_idpks.append(idpk)

        response = self.client.post(
            "/internal/messages",
            json={
                "idpk": idpk,
                "msgId": str(uuid4()),
                "type": "distance-table",
                "timestamp": timestamp,
                "sender": "central",
                "data": {
                    "distances": distances,
                },
            },
        )

        self.assertEqual(
            response.status_code,
            200,
        )

        self.assertEqual(
            response.json()["status"],
            "ok",
        )

    def test_connectivity_returns_latest_distance_table(self):
        self._publish_distance_table(
            timestamp="2199-01-01T10:00:00+00:00",
            distances={
                "ZZZ": {
                    "distance": 900,
                    "transportCost": 0.09,
                    "enabled": False,
                }
            },
        )

        self._publish_distance_table(
            timestamp="2199-01-02T10:00:00+00:00",
            distances={
                "TAR": {
                    "distance": 94306517,
                    "transportCost": 0.0013,
                    "enabled": True,
                },
                "HGW": {
                    "distance": 62763183,
                    "transportCost": 0.0034,
                    "enabled": True,
                },
            },
        )

        response = self.client.get(
            "/connectivity"
        )

        self.assertEqual(
            response.status_code,
            200,
        )

        body = response.json()

        self.assertEqual(
            body["total"],
            2,
        )

        self.assertTrue(
            body["timestamp"].startswith(
                "2199-01-02T10:00:00"
            )
        )

        self.assertEqual(
            body["items"],
            [
                {
                    "destination": "HGW",
                    "distance": 62763183.0,
                    "transportCost": 0.0034,
                    "enabled": True,
                },
                {
                    "destination": "TAR",
                    "distance": 94306517.0,
                    "transportCost": 0.0013,
                    "enabled": True,
                },
            ],
        )


if __name__ == "__main__":
    unittest.main()
