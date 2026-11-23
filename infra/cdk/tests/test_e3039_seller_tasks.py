"""E3-039: pruebas sin red ni PostgreSQL sobre el adaptador seller."""
import importlib
import sys
import unittest
from decimal import Decimal
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "lambda_src" / "saga_task"
sys.path.insert(0, str(SOURCE))
seller = importlib.import_module("seller_reservation")

BID = "820e9739-a3e6-4a6d-b4f4-7193a47d34ab"
OFFER = "KLD-cycle-1"


class Cursor:
    def __init__(self, *, mode="reserved", held=40, mutate=None):
        self.mode = mode
        self.held = held
        self.mutate = mutate
        self.queries = []
        self.last = ""

    def execute(self, sql, params):
        sql = " ".join(sql.split())
        self.queries.append((sql, params))
        self.last = sql
        if self.mutate is not None and "UPDATE " in sql:
            raise AssertionError("La Lambda no puede escribir reservas")

    def fetchone(self):
        q = self.last
        if q.startswith("SELECT offer_id FROM market_bids"):
            return (OFFER,)
        if q.startswith("SELECT cycle_id, city_id, is_local"):
            return ("cycle-1", "KLD", True, 100)
        if q.startswith("SELECT cycle_id, offer_id, quantity, status"):
            if self.mode == "rejected":
                return ("cycle-1", OFFER, 101, "REJECTED", "COR",
                        "QUANTITY_UNAVAILABLE", None)
            return ("cycle-1", OFFER, 40, "RESERVED", "COR", None, Decimal("4.25"))
        if q.startswith("SELECT status_payload FROM cycles"):
            return ({"market": {"minBid": 5}},)
        if q.startswith("SELECT offer_id, quantity, status FROM offer_reservations"):
            return (OFFER, 40, "HOLD")
        if q.startswith("SELECT cycle_id, role, counterpart_city_id"):
            return ("cycle-1", "PROPOSER", "COR", "KWH", Decimal("40"),
                    "CR", Decimal("170.00"), OFFER, "SUBMITTED", BID)
        if q.startswith("SELECT role, cycle_id, status FROM saga_executions"):
            return ("SELLER", "cycle-1", "RUNNING")
        if q.startswith("SELECT COALESCE(SUM(quantity)"):
            return (self.held,)
        if q.startswith("SELECT 1 FROM offer_reservations"):
            return None
        if q.startswith("SELECT 1 FROM swap_records"):
            return None
        if q.startswith("SELECT 1 FROM outbound_messages"):
            return None
        if q.startswith("SELECT b.msg_id"):
            return ("a5f4bf44-06bb-4fac-a5ec-e1bd1b35aa01", "cycle-1", OFFER,
                    101, "COR", "REJECTED", "QUANTITY_UNAVAILABLE", 100)
        raise AssertionError("Consulta desconocida: " + q)

    def fetchall(self):
        q = self.last
        if q.startswith("SELECT execution_name, cycle_id"):
            return [(BID, "cycle-1")]
        if q.startswith("SELECT payload, routing_key, cycle_id"):
            return [({"reason": "QUANTITY_UNAVAILABLE", "data": {
                "target": "a5f4bf44-06bb-4fac-a5ec-e1bd1b35aa01",
                "offerId": OFFER, "requested": 101, "available": 100,
            }}, "city.COR", "cycle-1")]
        raise AssertionError("Consulta desconocida: " + q)

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
    dsn = "postgresql://fake/test"

    def __init__(self, cursor):
        self.current = cursor
        self.connect = lambda _: Connection(self.current)


class SellerTaskTests(unittest.TestCase):
    def test_existing_reservation_is_read_only_and_idempotent(self):
        cursor = Cursor()
        store = Store(cursor)
        kwargs = dict(bid_idpk=BID, cycle_id="cycle-1", offer_id=OFFER,
                      quantity=40, saga_execution_id=BID)
        first = seller.verify_seller_reservation(store, **kwargs)
        second = seller.verify_seller_reservation(store, **kwargs)
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "RESERVED")
        self.assertTrue(first["alreadyReserved"])
        self.assertEqual(first["availableAfter"], 60)
        self.assertTrue(all(query.startswith("SELECT ") for query, _ in cursor.queries))

    def test_rejects_overbooking_without_changing_wallet_or_hold(self):
        cursor = Cursor(held=120)
        with self.assertRaisesRegex(seller.SellerReservationInconsistent,
                                    "SELLER_OVERBOOKED_OR_RELEASED"):
            seller.verify_seller_reservation(
                Store(cursor), bid_idpk=BID, cycle_id="cycle-1",
                offer_id=OFFER, quantity=40, saga_execution_id=BID,
            )
        self.assertTrue(all(query.startswith("SELECT ") for query, _ in cursor.queries))

    def test_rejected_bid_returns_existing_broker_outbox(self):
        cursor = Cursor(mode="rejected")
        first = seller.verify_seller_rejection(
            Store(cursor), bid_idpk=BID, reason="QUANTITY_UNAVAILABLE")
        second = seller.verify_seller_rejection(
            Store(cursor), bid_idpk=BID, reason="QUANTITY_UNAVAILABLE")
        self.assertEqual(first, second)
        self.assertTrue(first["outboxVerified"])
        self.assertTrue(all(query.startswith("SELECT ") for query, _ in cursor.queries))

    def test_late_execution_sees_previously_rejected_bid(self):
        cursor = Cursor(mode="rejected")
        with self.assertRaisesRegex(seller.SellerInsufficientEnergy,
                                    "SELLER_BID_ALREADY_REJECTED"):
            seller.verify_seller_reservation(
                Store(cursor), bid_idpk=BID, cycle_id="cycle-1",
                offer_id=OFFER, quantity=101, saga_execution_id=BID,
            )
        self.assertTrue(all(query.startswith("SELECT ") for query, _ in cursor.queries))

    def test_rejects_wrong_reason_or_input(self):
        with self.assertRaisesRegex(seller.SellerReservationInconsistent,
                                    "SELLER_REJECTION_REASON_MISMATCH"):
            seller.verify_seller_rejection(
                Store(Cursor()), bid_idpk=BID, reason="BID_TOO_SMALL")
        with self.assertRaises(ValueError):
            seller.verify_seller_reservation(
                Store(Cursor()), bid_idpk=BID, cycle_id="cycle-1",
                offer_id=OFFER, quantity=True, saga_execution_id=BID,
            )
        with self.assertRaisesRegex(seller.SellerReservationInconsistent,
                                    "SELLER_EXECUTION_IDPK_MISMATCH"):
            seller.verify_seller_reservation(
                Store(Cursor()), bid_idpk=BID, cycle_id="cycle-1",
                offer_id=OFFER, quantity=40, saga_execution_id="another-idpk",
            )


if __name__ == "__main__":
    unittest.main()
