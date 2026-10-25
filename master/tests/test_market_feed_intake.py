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
    def test_valid_feed_uses_atomic_market_persistence(self):
        payload = offer_payload()
        response_body = {
            "id": 1,
            "msgId": payload["msgId"],
            "idpk": payload["idpk"],
            "type": "offer",
            "cycleId": payload["cycleId"],
            "status": "PROCESSED",
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
            patch("app.routers.message_audit.persist_market_feed", return_value=sentinel.row) as persist,
            patch("app.routers.message_audit._inbound_message_to_out", return_value=response_body) as to_out,
        ):
            try:
                response = TestClient(app).post("/internal/audit/inbound/market", json=payload)
            finally:
                app.dependency_overrides.clear()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "PROCESSED")
        self.assertEqual(persist.call_args.args[1].type, "offer")
        self.assertEqual(persist.call_args.args[1].cityId, "HGW")
        self.assertEqual(persist.call_args.args[1].model_extra["futureField"], "preserved")
        to_out.assert_called_once_with(sentinel.session, sentinel.row)

    def test_bad_envelope_is_not_accepted_by_internal_route(self):
        payload = offer_payload()
        payload["idpk"] = payload["msgId"]
        with self.assertRaises(ValidationError):
            MarketFeedMessageIn.model_validate(payload)

    def test_invalid_offer_shape_and_closed_event_are_not_accepted(self):
        missing_quantity = offer_payload()
        missing_quantity["data"].pop("quantity")
        boolean_price = offer_payload()
        boolean_price["data"]["pricePerEnergy"] = True
        closed_with_offer = offer_payload()
        closed_with_offer.update(type="market-closed", sender="central", data={"offerId": "bad"})
        closed_with_offer.pop("cityId")
        for payload in (missing_quantity, boolean_price, closed_with_offer):
            with self.subTest(message_type=payload["type"]):
                with self.assertRaises(ValidationError):
                    MarketFeedMessageIn.model_validate(payload)


if __name__ == "__main__":
    unittest.main()
