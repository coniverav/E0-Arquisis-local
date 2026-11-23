"""E3-041: validación offline de la evidencia Wallet y outbox seller."""
import importlib
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "lambda_src" / "saga_task"
sys.path.insert(0, str(SOURCE))
accepted = importlib.import_module("seller_bid_accepted")

NOW = datetime(2026, 11, 23, 3, tzinfo=timezone.utc)
EXP = NOW + timedelta(seconds=45)
BID = "820e9739-a3e6-4a6d-b4f4-7193a47d34ab"
CYCLE = "cycle-e3041"
OFFER = "KLD-cycle-e3041"
SWAP = "swp-e3041"
TARGET = "5418bb8d-512a-4849-8203-fadc67cd1af4"
MSG = "db5ab1b4-b460-41ca-8986-5edbe2649fd7"
KEY = "35ccb022-0ef8-4484-9345-70de1a5d75ac"
TOKEN = "wallet-jwt-content-not-for-logs"


def iso(t):
    return t.isoformat().replace("+00:00", "Z")


class Cursor:
    def __init__(self, *, status="ACCEPTED", rows=True, expiry=EXP,
                 token=TOKEN, count=1, published=False):
        self.mode = status
        self.outbox = rows
        self.expiry = expiry
        self.token = token
        self.count = count
        self.published = published
        self.statements = []
        self.last = ""

    def execute(self, sql, params):
        self.last = " ".join(sql.split())
        self.statements.append((self.last, params))
        if not self.last.startswith("SELECT "):
            raise AssertionError("E3-041 no debe escribir estado económico ni outbox")

    def fetchone(self):
        q = self.last
        if "FROM saga_executions" in q:
            return ("SELLER", CYCLE, "WAITING")
        if "FROM market_bids" in q:
            return (TARGET, CYCLE, OFFER, "COR", 40, self.mode, SWAP, 4.25)
        if "FROM swap_records" in q:
            return (CYCLE, "PROPOSER", "COR", "KWH", 40, "CR", 170.0, OFFER,
                    "PROPOSED", SWAP, TOKEN, EXP, BID)
        if "FROM offer_reservations" in q:
            return (OFFER, 40, "HOLD")
        raise AssertionError("Consulta inesperada: " + q)

    def fetchall(self):
        if "FROM outbound_messages" not in self.last:
            raise AssertionError("Consulta inesperada: " + self.last)
        if not self.outbox:
            return []
        envelope = {
            "type": "bid-accepted", "cityId": "KLD", "cycleId": CYCLE,
            "msgId": MSG, "idpk": KEY,
            "data": {"target": TARGET, "offerId": OFFER, "quantity": 40,
                     "pricePerEnergy": 4.25, "amount": 170.0,
                     "swapId": SWAP, "swapToken": self.token,
                     "expiresAt": iso(self.expiry)},
        }
        result = (MSG, KEY, envelope, "city.COR", "energy.x",
                  "PUBLISHED" if self.published else "PENDING", True,
                  CYCLE, TARGET, self.expiry, "BROKER")
        return [result] * self.count

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class Connection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class Store:
    city_id = "KLD"
    dsn = "postgresql://fake/db"

    def __init__(self, cursor=None, at=NOW):
        self.cursor = cursor or Cursor()
        self.now = lambda: at
        self.connect = lambda dsn: Connection(self.cursor)


def kwargs():
    return dict(bid_idpk=BID, cycle_id=CYCLE, swap_id=SWAP,
                expires_at=iso(EXP), saga_execution_id=BID)


class SellerBidAcceptedContractTests(unittest.TestCase):
    def test_verified_event_outbox_is_read_only_and_idempotent(self):
        store = Store()
        first = accepted.verify_persisted_bid_accepted(store, **kwargs())
        second = accepted.verify_persisted_bid_accepted(store, **kwargs())
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "QUEUED")
        self.assertTrue(first["alreadyEnqueued"])
        self.assertNotIn(TOKEN, json.dumps(first))
        self.assertEqual(len(store.cursor.statements), 10)

    def test_published_outbox_is_not_recreated(self):
        result = accepted.verify_persisted_bid_accepted(
            Store(Cursor(published=True)), **kwargs()
        )
        self.assertEqual(result["status"], "PUBLISHED")

    def test_http_202_without_event_cannot_publish(self):
        store = Store(Cursor(status="RESERVED", rows=False))
        with self.assertRaises(accepted.SellerBidAcceptedPending):
            accepted.verify_persisted_bid_accepted(store, **kwargs())
        self.assertTrue(all(q.startswith("SELECT ") for q, _ in store.cursor.statements))

    def test_proposed_without_outbox_cannot_publish(self):
        with self.assertRaises(accepted.SellerBidAcceptedPending):
            accepted.verify_persisted_bid_accepted(Store(Cursor(rows=False)), **kwargs())

    def test_different_token_or_expiry_fails_closed(self):
        with self.assertRaises(accepted.SellerBidAcceptedInconsistent):
            accepted.verify_persisted_bid_accepted(
                Store(Cursor(token="forged-token")), **kwargs()
            )
        wrong = kwargs()
        wrong["expires_at"] = iso(EXP + timedelta(seconds=15))
        with self.assertRaises(accepted.SellerBidAcceptedInconsistent):
            accepted.verify_persisted_bid_accepted(Store(), **wrong)

    def test_duplicate_outbox_fails_closed(self):
        with self.assertRaises(accepted.SellerBidAcceptedInconsistent):
            accepted.verify_persisted_bid_accepted(
                Store(Cursor(count=2)), **kwargs()
            )

    def test_expired_token_does_not_pass(self):
        with self.assertRaises(accepted.SellerBidAcceptedPending):
            accepted.verify_persisted_bid_accepted(
                Store(at=EXP + timedelta(seconds=1)), **kwargs()
            )

    def test_identity_mismatch_rejected_before_sql(self):
        wrong = kwargs()
        wrong["saga_execution_id"] = "other-saga"
        store = Store()
        with self.assertRaises(accepted.SellerBidAcceptedInconsistent):
            accepted.verify_persisted_bid_accepted(store, **wrong)
        self.assertEqual(store.cursor.statements, [])

    def test_lambda_routes_without_wallet_http(self):
        handler = importlib.import_module("handler")
        store = Store()
        with patch.object(handler, "_build_store", return_value=store), \
             patch.dict("os.environ", {"SAGA_TASK": "publish-bid-accepted"}):
            answer = handler.lambda_handler({
                "bidIdpk": BID, "cycleId": CYCLE, "swapId": SWAP,
                "expiresAt": iso(EXP), "sagaExecutionId": BID,
            }, object())
        self.assertEqual(answer["status"], "QUEUED")

    def test_asl_passes_identity_and_retries_event_commit_race(self):
        definition = json.loads((
            Path(__file__).resolve().parents[1] / "energyshark_infra"
            / "asl" / "seller.asl.json"
        ).read_text())
        task = definition["States"]["PublishBidAccepted"]
        self.assertEqual(task["Parameters"]["Payload"]["swapId.$"],
                         "$.proposalDetails.swapId")
        self.assertNotIn("swapToken.$", task["Parameters"]["Payload"])
        self.assertTrue(any("SellerBidAcceptedPending" in r["ErrorEquals"]
                            for r in task["Retry"]))


if __name__ == "__main__":
    unittest.main()
