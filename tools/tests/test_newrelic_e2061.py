"""E2-061: contratos locales del dashboard y de las condiciones NRQL."""
import importlib.util
from pathlib import Path
import unittest

MODULE_PATH = Path(__file__).resolve().parents[1] / "newrelic_e2061.py"
spec = importlib.util.spec_from_file_location("newrelic_e2061", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ProvisioningTests(unittest.TestCase):
    def test_dashboard_covers_four_signals_and_market_consumption(self):
        board = module.dashboard_definition(1234)
        widgets = board["pages"][0]["widgets"]
        self.assertEqual(len(widgets), 8)
        self.assertEqual(board["name"], module.DASHBOARD_NAME)
        queries = [item["rawConfiguration"]["nrqlQueries"][0]["query"] for item in widgets]
        all_queries = " ".join(queries)
        for marker in ("timeout", "statusCode = 429", "retryAfterSeconds",
                       "state = 'retrying'", "consumer = 'market'",
                       "outcome = 'broker_reject'", "EnergySharkBrokerHeartbeat"):
            self.assertIn(marker, all_queries)
        for widget in widgets:
            self.assertEqual(widget["rawConfiguration"]["nrqlQueries"][0]["accountIds"], [1234])
            self.assertLessEqual(widget["layout"]["column"] + widget["layout"]["width"] - 1, 12)
        self.assertNotIn("X-Api-Key", all_queries)
        self.assertNotIn("RABBITMQ_URL", all_queries)

    def test_alerts_are_distinct_and_correlate_with_events(self):
        conditions = module.condition_definitions()
        self.assertEqual(len(conditions), 5)
        self.assertEqual(len({condition["name"] for condition in conditions}), 5)
        for rule in conditions:
            self.assertTrue(rule["enabled"])
            self.assertIn("SELECT count(*) FROM", rule["nrql"]["query"])
            self.assertNotIn("SINCE", rule["nrql"]["query"])
            self.assertNotIn("TIMESERIES", rule["nrql"]["query"])
        heartbeat = conditions[-1]
        self.assertEqual(heartbeat["expiration"]["expirationDuration"], 300)
        self.assertTrue(heartbeat["expiration"]["openViolationOnExpiration"])
        self.assertEqual(str(heartbeat["terms"]["priority"]), "CRITICAL")

    def test_serialization_preserves_graphql_enums_and_escapes_text(self):
        result = module.graphql_value({"permission": module.EnumValue("PUBLIC_READ_ONLY"),
                                       "text": 'say "hello"', "count": 2, "enabled": True})
        self.assertIn("permission: PUBLIC_READ_ONLY", result)
        self.assertIn('text: "say \\"hello\\""', result)
        self.assertIn("enabled: true", result)
        self.assertNotIn('"PUBLIC_READ_ONLY"', result)

    def test_existing_policy_and_alerts_are_reused(self):
        class FakeClient:
            def __init__(self):
                self.queries = []
            def execute(self, query):
                self.queries.append(query)
                if "policiesSearch" in query:
                    return {"actor": {"account": {"alerts": {"policiesSearch": {
                        "policies": [{"name": module.POLICY_NAME, "id": "500"}]
                    }}}}}
                if "nrqlConditionsSearch" in query:
                    return {"actor": {"account": {"alerts": {"nrqlConditionsSearch": {
                        "nrqlConditions": [{"name": item["name"]} for item in module.condition_definitions()],
                        "nextCursor": None
                    }}}}}
                raise AssertionError("No se debían crear nuevas entidades")
        fake = FakeClient()
        self.assertEqual(module.provision_alerts(fake, 1234), 500)
        self.assertEqual(len(fake.queries), 2)

    def test_dry_run_does_not_use_network(self):
        from contextlib import redirect_stdout
        from io import StringIO
        output = StringIO()
        with redirect_stdout(output):
            module.main(["--account-id", "1234"])
        self.assertIn("Dry-run", output.getvalue())
        self.assertIn("EnergySharkWalletHttp", output.getvalue())


if __name__ == "__main__":
    unittest.main()
