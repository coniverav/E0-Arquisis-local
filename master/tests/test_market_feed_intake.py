import os
import unittest
from unittest.mock import patch, sentinel
from uuid import uuid4

from fastapi.testclient import TestClient
from pydantic import ValidationError

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://test:test@localhost/test")

from app.database import get_session
from app.main import app
from app.schemas import MarketFeedMessageIn


def offer_payload():
    return {
        "idpk": str(uuid4()),
        "msgId": str(uuid4()),
        "type": "offer",
        "timestamp": "2026-10-24T12:00:00Z",
        "cycleId": "cycle-9431",
        "cityId": "HGW",
        "data": {"offerId": "HGW-9431", "quantity": 20, "pricePerEnergy": 330},
        "futureField": "preserved",
    }


class MarketFeedIntakeTests(unittest.TestCase):
    def test_valid_feed_is_recorded_as_received_without_business_processing(self):
        payload = offer_payload()
        response_body = {
            "id": 1,
            "msgId": payload["msgId"],
            "idpk": payload["idpk"],
            "type": "offer",
            "cycleId": payload["cycleId"],
            "status": "RECEIVED",
            "reasonCode": None,
            "reason": None,
            "receivedAt": payload["timestamp"],
            "processedAt": None,
            "relatedMsgId": payload["msgId"],
            "payload": payload,
            "rawPayload": None,
        }
        app.dependency_overrides[get_session] = lambda: sentinel.session
        with (
            patch("app.routers.message_audit.record_inbound_message", return_value=sentinel.row) as record,
            patch("app.routers.message_audit._inbound_message_to_out", return_value=response_body) as to_out,
        ):
            try:
                response = TestClient(app).post("/internal/audit/inbound/market", json=payload)
            finally:
                app.dependency_overrides.clear()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "RECEIVED")
        self.assertEqual(record.call_args.kwargs["message_type"], "offer")
        self.assertEqual(record.call_args.kwargs["sender"], "HGW")
        self.assertEqual(record.call_args.kwargs["payload"]["futureField"], "preserved")
        to_out.assert_called_once_with(sentinel.session, sentinel.row)

    def test_bad_envelope_is_not_accepted_by_internal_route(self):
        payload = offer_payload()
        payload["idpk"] = payload["msgId"]
        with self.assertRaises(ValidationError):
            MarketFeedMessageIn.model_validate(payload)


if __name__ == "__main__":
    unittest.main()
