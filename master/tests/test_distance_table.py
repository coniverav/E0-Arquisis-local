import unittest
from datetime import datetime, timezone
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlmodel import Session, select

from app.database import engine, run_migrations
from app.main import app
from app.models import DistanceTable, InboundMessage, ProcessedIdpk


class DistanceTableTests(unittest.TestCase):

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

    def _payload(
        self,
        *,
        timestamp: str,
        distances: dict,
        idpk: str | None = None,
        msg_id: str | None = None,
    ):
        idpk = idpk or str(uuid4())
        msg_id = msg_id or str(uuid4())

        if idpk not in self.created_idpks:
            self.created_idpks.append(idpk)

        return {
            "idpk": idpk,
            "msgId": msg_id,
            "type": "distance-table",
            "timestamp": timestamp,
            "sender": "central",
            "data": {
                "distances": distances,
            },
        }

    def test_latest_distance_table_is_persisted_and_queryable(self):
        first_idpk = str(uuid4())
        first_msg_id = str(uuid4())

        first_distances = {
            "city-a": {
                "distance": 100,
                "transportCost": 10,
                "enabled": True,
            },
            "city-b": {
                "distance": 250,
                "transportCost": 20,
                "enabled": False,
            },
        }

        first_payload = self._payload(
            idpk=first_idpk,
            msg_id=first_msg_id,
            timestamp="2099-01-01T10:00:00+00:00",
            distances=first_distances,
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

        # La primera tabla debe poder consultarse.
        current_response = self.client.get(
            "/distance-table"
        )

        self.assertEqual(
            current_response.status_code,
            200,
        )

        current = current_response.json()

        self.assertEqual(
            current["idpk"],
            first_idpk,
        )

        self.assertEqual(
            current["msgId"],
            first_msg_id,
        )

        self.assertEqual(
            current["distances"],
            first_distances,
        )

        # ---------------------------------------------------------
        # Una tabla recibida después, pero con timestamp anterior,
        # NO debe reemplazar a la tabla vigente.
        # ---------------------------------------------------------
        older_distances = {
            "city-a": {
                "distance": 999,
                "transportCost": 99,
                "enabled": False,
            }
        }

        older_payload = self._payload(
            timestamp="2098-12-31T10:00:00+00:00",
            distances=older_distances,
        )

        older_response = self.client.post(
            "/internal/messages",
            json=older_payload,
        )

        self.assertEqual(
            older_response.status_code,
            200,
        )

        current_response = self.client.get(
            "/distance-table"
        )

        current = current_response.json()

        self.assertEqual(
            current["idpk"],
            first_idpk,
        )

        self.assertEqual(
            current["distances"],
            first_distances,
        )

        # ---------------------------------------------------------
        # Una tabla con timestamp más reciente pasa a ser la vigente.
        # ---------------------------------------------------------
        newest_idpk = str(uuid4())
        newest_msg_id = str(uuid4())

        newest_distances = {
            "city-a": {
                "distance": 80,
                "transportCost": 8,
                "enabled": True,
            },
            "city-c": {
                "distance": 300,
                "transportCost": 25,
                "enabled": True,
            },
        }

        newest_payload = self._payload(
            idpk=newest_idpk,
            msg_id=newest_msg_id,
            timestamp="2099-01-02T10:00:00+00:00",
            distances=newest_distances,
        )

        newest_response = self.client.post(
            "/internal/messages",
            json=newest_payload,
        )

        self.assertEqual(
            newest_response.status_code,
            200,
        )

        current_response = self.client.get(
            "/distance-table"
        )

        self.assertEqual(
            current_response.status_code,
            200,
        )

        current = current_response.json()

        self.assertEqual(
            current["idpk"],
            newest_idpk,
        )

        self.assertEqual(
            current["msgId"],
            newest_msg_id,
        )

        self.assertEqual(
            current["distances"],
            newest_distances,
        )

        # ---------------------------------------------------------
        # Retry de la misma operación:
        # mismo idpk, nuevo msgId.
        #
        # No debe crear una nueva versión ni reemplazar el contenido.
        # ---------------------------------------------------------
        retry_payload = self._payload(
            idpk=newest_idpk,
            msg_id=str(uuid4()),
            timestamp="2100-01-01T10:00:00+00:00",
            distances={
                "city-x": {
                    "distance": 9999,
                    "transportCost": 999,
                    "enabled": False,
                }
            },
        )

        retry_response = self.client.post(
            "/internal/messages",
            json=retry_payload,
        )

        self.assertEqual(
            retry_response.status_code,
            200,
        )

        with Session(engine) as session:
            tables = session.exec(
                select(DistanceTable).where(
                    DistanceTable.idpk.in_(
                        self.created_idpks
                    )
                )
            ).all()

            # Primera + antigua + nueva.
            # El retry no genera una cuarta tabla.
            self.assertEqual(
                len(tables),
                3,
            )

            newest_rows = [
                table
                for table in tables
                if table.idpk == newest_idpk
            ]

            self.assertEqual(
                len(newest_rows),
                1,
            )

            self.assertEqual(
                newest_rows[0].msg_id,
                newest_msg_id,
            )

            inbound = session.exec(
                select(InboundMessage)
                .where(
                    InboundMessage.idpk
                    == newest_idpk
                )
                .order_by(
                    InboundMessage.id
                )
            ).all()

            self.assertEqual(
                len(inbound),
                2,
            )

            self.assertEqual(
                inbound[0].status,
                "PROCESSED",
            )

            self.assertEqual(
                inbound[1].status,
                "DUPLICATE",
            )

        # El retry tampoco cambia la tabla vigente.
        final_response = self.client.get(
            "/distance-table"
        )

        self.assertEqual(
            final_response.status_code,
            200,
        )

        final_table = final_response.json()

        self.assertEqual(
            final_table["idpk"],
            newest_idpk,
        )

        self.assertEqual(
            final_table["msgId"],
            newest_msg_id,
        )

        self.assertEqual(
            final_table["distances"],
            newest_distances,
        )


if __name__ == "__main__":
    unittest.main()