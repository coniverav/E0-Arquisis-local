import importlib.util
import json
import os
from collections import Counter
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from aws_cdk.assertions import Template

from app import build_app
from energyshark_infra.stacks.sagas import LOG_DELIVERY_ACTIONS


class ServerlessInfrastructureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app, cls.stacks = build_app(
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
        cls.template = Template.from_stack(cls.stacks.sagas).to_json()

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
        for name, machine in machines.items():
            properties = machine["Properties"]
            self.assertEqual(properties["StateMachineType"], "STANDARD")
            self.assertEqual(properties["LoggingConfiguration"]["Level"], "ALL")
            self.assertFalse(
                properties["LoggingConfiguration"]["IncludeExecutionData"]
            )
            self.assertTrue(properties["DefinitionSubstitutions"])
            definition = json.loads(properties["DefinitionString"])
            self.assertNotIn("StateMachineName", properties)
            self.assertEqual(properties["EncryptionConfiguration"], {"Type": "AWS_OWNED_KEY"})
            timeouts[name] = definition["TimeoutSeconds"]
            destination, = properties["LoggingConfiguration"]["Destinations"]
            group, attribute = destination["CloudWatchLogsLogGroup"]["LogGroupArn"]["Fn::GetAtt"]
            self.assertIn(group, self.resources("AWS::Logs::LogGroup"))
            self.assertEqual(attribute, "Arn")
            role, _ = properties["RoleArn"]["Fn::GetAtt"]
            policy_id, = (
                key for key, policy in self.resources("AWS::IAM::Policy").items()
                if {"Ref": role} in policy["Properties"]["Roles"]
            )
            self.assertIn(policy_id, machine["DependsOn"])
            statements = self.template["Resources"][policy_id]["Properties"]["PolicyDocument"]["Statement"]
            invoke, = (st for st in statements if st["Action"] == "lambda:InvokeFunction")
            self.assertCountEqual(invoke["Resource"], properties["DefinitionSubstitutions"].values())

        self.assertEqual(timeouts["SellerStateMachine"], 900)
        self.assertEqual(timeouts["BuyerStateMachine"], 1200)
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
            self.assertEqual(properties["PackageType"], "Image")
            self.assertEqual(properties["Timeout"], 20)
            self.assertEqual(properties["MemorySize"], 384)
            variables = properties["Environment"]["Variables"]
            self.assertEqual(variables["ENERGYSHARK_ENV"], "dev")
            tasks.add(variables["SAGA_TASK"])
            self.assertIn("ImageUri", properties["Code"])
            self.assertNotIn("FunctionName", properties)
            self.assertEqual(properties["Architectures"], ["x86_64"])
            group = properties["LoggingConfig"]["LogGroup"]["Ref"]
            log = self.resources("AWS::Logs::LogGroup")[group]
            self.assertEqual(log["Properties"]["RetentionInDays"], 7)
            self.assertEqual(log["DeletionPolicy"], "Retain")
            self.assertNotIn("LogGroupName", log["Properties"])

        self.assertIn("seller-proposal", tasks)
        self.assertIn("buyer-acceptance", tasks)
        self.assertIn("finalize-saga", tasks)

    def test_secret_parameters_have_no_values_in_template(self) -> None:
        parameters = self.template["Parameters"]
        for name in ("DatabaseSecretArn", "WalletSecretArn"):
            self.assertEqual(parameters[name]["Type"], "String")
            self.assertTrue(parameters[name]["NoEcho"])
            self.assertNotIn("Default", parameters[name])
            pattern = parameters[name]["AllowedPattern"]
            arn = "arn:aws:secretsmanager:us-east-2:111122223333:secret:dev/db-aBc123"
            self.assertRegex(arn, f"^{pattern}$")
            for wildcard in ("*", arn[:-6] + "*"):
                self.assertNotRegex(wildcard, f"^{pattern}$")

        self.assertNotIn("BrokerSecretArn", parameters)
        rendered = json.dumps(self.template)
        self.assertNotIn("x-api-key", rendered.lower())
        self.assertNotIn("postgresql://", rendered.lower())
        self.assertNotIn("amqps://", rendered.lower())

    def test_iam_uses_only_required_actions_and_justified_wildcards(self) -> None:
        allowed_wildcards = {
            frozenset(LOG_DELIVERY_ACTIONS),
            frozenset(("states:SendTaskSuccess", "states:SendTaskFailure")),
            frozenset(("states:SendTaskSuccess",)),
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
                        ("DatabaseSecretArn", "WalletSecretArn"),
                    )

        self.assertTrue(start_execution_found)

    def test_connector_role_is_assumable_only_by_ec2(self) -> None:
        roles = self.resources("AWS::IAM::Role")
        profile, = self.resources("AWS::IAM::InstanceProfile").values()
        role_ref, = profile["Properties"]["Roles"]
        role = roles[role_ref["Ref"]]["Properties"]
        self.assertNotIn("RoleName", role)
        self.assertNotIn("InstanceProfileName", profile["Properties"])
        principal = role["AssumeRolePolicyDocument"]["Statement"][0]["Principal"]
        self.assertEqual(principal, {"Service": "ec2.amazonaws.com"})

    def test_outputs_are_only_arns_of_managed_resources(self):
        expected = {
            "AWS::Lambda::Function": 13,
            "AWS::StepFunctions::StateMachine": 2,
            "AWS::StepFunctions::StateMachineAlias": 2,
            "AWS::IAM::Role": 1,
            "AWS::IAM::InstanceProfile": 1,
        }
        types = Counter()
        targets = set()
        for name, output in self.template["Outputs"].items():
            self.assertTrue(name.endswith("Arn"))
            value = output["Value"]
            if "Ref" in value:
                target = value["Ref"]
                self.assertIn(target, self.resources("AWS::StepFunctions::StateMachineAlias"))
            else:
                target, attribute = value["Fn::GetAtt"]
                self.assertEqual(attribute, "Arn")
            self.assertNotIn(target, targets)
            targets.add(target)
            types[self.template["Resources"][target]["Type"]] += 1
        self.assertEqual(types, expected)


class SagaTaskArtifactTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "lambda_src"
            / "saga_task"
        )
        sys.path.insert(0, str(source))
        handler_path = source / "handler.py"
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
        with patch.dict(os.environ, {"SAGA_TASK": "finalize-saga"}, clear=False):
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
