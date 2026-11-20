import importlib.util
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from aws_cdk.assertions import Template

from app import build_app
from energyshark_infra.stacks.sagas import LOG_DELIVERY_ACTIONS


class ServerlessInfrastructureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app, self.stacks = build_app(
            {
                "environment": "dev",
                "environments": {
                    "dev": {
                        "stackPrefix": "EnergySharkDev",
                        "sellerSagaTimeoutSeconds": 900,
                        "buyerSagaTimeoutSeconds": 1200,
                        "lambdaTimeoutSeconds": 20,
                        "lambdaMemoryMb": 384,
                        "logRetentionDays": 7,
                    }
                },
            }
        )
        self.template = Template.from_stack(self.stacks.sagas).to_json()

    def resources(self, resource_type: str) -> dict:
        return {
            logical_id: resource
            for logical_id, resource in self.template["Resources"].items()
            if resource["Type"] == resource_type
        }

    def test_declares_two_versioned_standard_machines(self) -> None:
        machines = self.resources("AWS::StepFunctions::StateMachine")
        self.assertEqual(len(machines), 2)
        self.assertEqual(
            len(self.resources("AWS::StepFunctions::StateMachineVersion")),
            2,
        )
        aliases = self.resources("AWS::StepFunctions::StateMachineAlias")
        self.assertEqual(len(aliases), 2)

        timeouts = {}
        for machine in machines.values():
            properties = machine["Properties"]
            self.assertEqual(properties["StateMachineType"], "STANDARD")
            self.assertEqual(properties["LoggingConfiguration"]["Level"], "ALL")
            self.assertFalse(
                properties["LoggingConfiguration"]["IncludeExecutionData"]
            )
            self.assertTrue(properties["DefinitionSubstitutions"])
            definition = json.loads(properties["DefinitionString"])
            timeouts[properties["StateMachineName"]] = definition["TimeoutSeconds"]

        self.assertEqual(timeouts["energyshark-dev-seller-saga"], 900)
        self.assertEqual(timeouts["energyshark-dev-buyer-saga"], 1200)
        for alias in aliases.values():
            properties = alias["Properties"]
            self.assertEqual(properties["Name"], "live")
            self.assertEqual(properties["RoutingConfiguration"][0]["Weight"], 100)

    def test_declares_reproducible_task_lambdas_by_environment(self) -> None:
        functions = self.resources("AWS::Lambda::Function")
        self.assertEqual(len(functions), 13)
        tasks = set()
        for function in functions.values():
            properties = function["Properties"]
            self.assertEqual(properties["Runtime"], "python3.12")
            self.assertEqual(properties["Handler"], "handler.lambda_handler")
            self.assertEqual(properties["Timeout"], 20)
            self.assertEqual(properties["MemorySize"], 384)
            variables = properties["Environment"]["Variables"]
            self.assertEqual(variables["ENERGYSHARK_ENV"], "dev")
            tasks.add(variables["SAGA_TASK"])
            self.assertIn("S3Bucket", properties["Code"])
            self.assertIn("S3Key", properties["Code"])

        self.assertIn("seller-proposal", tasks)
        self.assertIn("buyer-acceptance", tasks)
        self.assertIn("finalize-saga", tasks)

    def test_secret_parameters_have_no_values_in_template(self) -> None:
        parameters = self.template["Parameters"]
        for name in ("DatabaseSecretArn", "WalletSecretArn", "BrokerSecretArn"):
            self.assertEqual(parameters[name]["Type"], "String")
            self.assertTrue(parameters[name]["NoEcho"])
            self.assertNotIn("Default", parameters[name])

        rendered = json.dumps(self.template)
        self.assertNotIn("x-api-key", rendered.lower())
        self.assertNotIn("postgresql://", rendered.lower())
        self.assertNotIn("amqps://", rendered.lower())

    def test_iam_uses_only_required_actions_and_justified_wildcards(self) -> None:
        allowed_wildcards = {
            frozenset(LOG_DELIVERY_ACTIONS),
            frozenset(("states:SendTaskSuccess", "states:SendTaskFailure")),
        }
        policies = self.resources("AWS::IAM::Policy")
        self.assertTrue(policies)
        start_execution_found = False

        for policy in policies.values():
            for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
                actions = statement["Action"]
                if isinstance(actions, str):
                    actions = [actions]
                self.assertNotIn("*", actions)
                if statement["Resource"] == "*":
                    self.assertIn(frozenset(actions), allowed_wildcards)
                if "states:StartExecution" in actions:
                    start_execution_found = True
                    resources = statement["Resource"]
                    self.assertEqual(
                        resources,
                        [
                            {"Ref": "SellerStateMachineAlias"},
                            {"Ref": "BuyerStateMachineAlias"},
                        ],
                    )
                if "secretsmanager:GetSecretValue" in actions:
                    self.assertIsInstance(statement["Resource"], dict)
                    self.assertIn(
                        statement["Resource"]["Ref"],
                        ("DatabaseSecretArn", "WalletSecretArn", "BrokerSecretArn"),
                    )

        self.assertTrue(start_execution_found)

    def test_connector_role_is_assumable_only_by_ec2(self) -> None:
        roles = self.resources("AWS::IAM::Role")
        connector_roles = [
            role
            for role in roles.values()
            if role["Properties"].get("RoleName")
            == "energyshark-dev-connector-sagas"
        ]
        self.assertEqual(len(connector_roles), 1)
        principal = connector_roles[0]["Properties"]["AssumeRolePolicyDocument"][
            "Statement"
        ][0]["Principal"]
        self.assertEqual(principal, {"Service": "ec2.amazonaws.com"})
        profiles = self.resources("AWS::IAM::InstanceProfile")
        self.assertEqual(len(profiles), 1)
        profile = next(iter(profiles.values()))["Properties"]
        self.assertEqual(
            profile["InstanceProfileName"],
            "energyshark-dev-connector-sagas",
        )


class SagaTaskArtifactTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        handler_path = (
            Path(__file__).resolve().parents[1]
            / "lambda_src"
            / "saga_task"
            / "handler.py"
        )
        spec = importlib.util.spec_from_file_location("saga_task_handler", handler_path)
        assert spec and spec.loader
        cls.handler = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.handler)

    def test_handler_fails_closed_without_logging_sensitive_payload(self) -> None:
        event = {
            "idpk": "operation-1",
            "cycleId": "cycle-1",
            "taskToken": "sensitive-task-token",
            "swapToken": "sensitive-wallet-token",
            "jwt": "sensitive-jwt",
        }
        context = type("Context", (), {"aws_request_id": "request-1"})()
        with patch.dict(os.environ, {"SAGA_TASK": "seller-proposal"}, clear=False):
            with self.assertLogs(self.handler.logger, level="INFO") as captured:
                with self.assertRaises(self.handler.SagaTaskNotImplemented):
                    self.handler.lambda_handler(event, context)

        rendered = "\n".join(captured.output)
        self.assertIn("operation-1", rendered)
        self.assertIn("cycle-1", rendered)
        self.assertNotIn("sensitive-task-token", rendered)
        self.assertNotIn("sensitive-wallet-token", rendered)
        self.assertNotIn("sensitive-jwt", rendered)


if __name__ == "__main__":
    unittest.main()
