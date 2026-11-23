"""E3-043: guardia offline sobre serialización de eventos Wallet.

Las pruebas económicas y de publicación se ejecutan en PostgreSQL.
"""
import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "master" / "app" / "services" / "wallet_service.py"


class WalletSwapSerializationTests(unittest.TestCase):
    def test_every_swap_lookup_in_event_handler_locks_row(self):
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        service = next(n for n in tree.body if isinstance(n, ast.ClassDef)
                       and n.name == "WalletService")
        handler = next(n for n in service.body if isinstance(n, ast.FunctionDef)
                       and n.name == "process_swap_event")
        calls = [n for n in ast.walk(handler) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "with_for_update"]
        # swap_id, idpk, accept_idempotency_key e idempotency_key.
        self.assertGreaterEqual(len(calls), 4)

    def test_settlement_owns_offer_update_and_releases_only_on_terminal(self):
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        source = ast.get_source_segment(SOURCE.read_text(encoding="utf-8"),
                                        next(n for n in ast.walk(tree)
                                             if isinstance(n, ast.FunctionDef)
                                             and n.name == "_update_offer_and_reservation"))
        self.assertIn('res.status = "COMMITTED"', source)
        self.assertIn('enqueue_confirmed_offer_update(session, offer, now=now)', source)
        self.assertIn('res.status = "RELEASED"', source)


if __name__ == "__main__":
    unittest.main()
