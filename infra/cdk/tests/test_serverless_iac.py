import importlib.util
import json
from fnmatch import fnmatchcase
import os
from collections import Counter
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from aws_cdk.assertions import Template

from app import build_app
from energyshark_infra.stacks.sagas import (
    LAMBDA_VPC_ENI_ACTIONS,
    LOG_DELIVERY_ACTIONS,
)


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
            self.assertEqual(properties["ReservedConcurrentExecutions"], 2)
            variables = properties["Environment"]["Variables"]
            self.assertEqual(variables["ENERGYSHARK_ENV"], "dev")
            tasks.add(variables["SAGA_TASK"])
            self.assertIn("ImageUri", properties["Code"])
            self.assertNotIn("FunctionName", properties)
            self.assertEqual(properties["Architectures"], ["x86_64"])
            self.assertEqual(
                properties["VpcConfig"]["SubnetIds"],
                [{"Ref": "ExistingSubnetId"}],
            )
            self.assertEqual(len(properties["VpcConfig"]["SecurityGroupIds"]), 1)
            self.assertIn("HTTPS_PROXY", variables)
            self.assertIn("NO_PROXY", variables)
            group = properties["LoggingConfig"]["LogGroup"]["Ref"]
            log = self.resources("AWS::Logs::LogGroup")[group]
            self.assertEqual(log["Properties"]["RetentionInDays"], 7)
            self.assertEqual(log["DeletionPolicy"], "Retain")
            self.assertNotIn("LogGroupName", log["Properties"])

        self.assertIn("seller-proposal", tasks)
        self.assertIn("buyer-acceptance", tasks)
        self.assertIn("finalize-saga", tasks)

    def test_secret_resources_have_only_generated_bootstrap_values(self) -> None:
        secrets = self.resources("AWS::SecretsManager::Secret")
        self.assertEqual(len(secrets), 2)
        for resource in secrets.values():
            properties = resource["Properties"]
            self.assertNotIn("SecretString", properties)
            generated = properties["GenerateSecretString"]
            template = json.loads(generated["SecretStringTemplate"])
            self.assertFalse(template["configured"])
            self.assertIn(template["kind"], {"database", "wallet"})
            self.assertEqual(generated["GenerateStringKey"], "bootstrapNonce")
            self.assertEqual(resource["DeletionPolicy"], "Retain")

        rendered = json.dumps(self.template)
        self.assertNotIn("x-api-key", rendered.lower())
        self.assertNotIn("postgresql://", rendered.lower())
        self.assertNotIn("amqps://", rendered.lower())

    def test_iam_uses_only_required_actions_and_justified_wildcards(self) -> None:
        allowed_wildcards = {
            frozenset(LOG_DELIVERY_ACTIONS),
            frozenset(LAMBDA_VPC_ENI_ACTIONS),
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
                        self.resources("AWS::SecretsManager::Secret"),
                    )

        self.assertTrue(start_execution_found)

    def test_all_lambda_roles_have_only_the_required_vpc_eni_actions(self) -> None:
        functions = self.resources("AWS::Lambda::Function")
        policies = self.resources("AWS::IAM::Policy")
        lambda_role_refs = {
            function["Properties"]["Role"]["Fn::GetAtt"][0]
            for function in functions.values()
        }
        self.assertEqual(len(functions), 13)
        self.assertEqual(len(lambda_role_refs), 13)

        ec2_actions_by_role = {}
        for policy in policies.values():
            statements = policy["Properties"]["PolicyDocument"]["Statement"]
            ec2_actions = {
                action
                for statement in statements
                for action in (
                    [statement["Action"]]
                    if isinstance(statement["Action"], str)
                    else statement["Action"]
                )
                if action.startswith("ec2:")
            }
            if not ec2_actions:
                continue
            ec2_statements = [
                statement
                for statement in statements
                if any(
                    action.startswith("ec2:")
                    for action in (
                        [statement["Action"]]
                        if isinstance(statement["Action"], str)
                        else statement["Action"]
                    )
                )
            ]
            self.assertTrue(
                all(statement["Resource"] == "*" for statement in ec2_statements)
            )
            for role in policy["Properties"]["Roles"]:
                ec2_actions_by_role.setdefault(role["Ref"], set()).update(
                    ec2_actions
                )

        self.assertEqual(set(ec2_actions_by_role), lambda_role_refs)
        for role in lambda_role_refs:
            self.assertEqual(
                ec2_actions_by_role[role],
                set(LAMBDA_VPC_ENI_ACTIONS),
            )

        profile, = self.resources("AWS::IAM::InstanceProfile").values()
        connector_role, = profile["Properties"]["Roles"]
        self.assertNotIn(connector_role["Ref"], ec2_actions_by_role)

        state_machine_roles = {
            machine["Properties"]["RoleArn"]["Fn::GetAtt"][0]
            for machine in self.resources(
                "AWS::StepFunctions::StateMachine"
            ).values()
        }
        self.assertTrue(state_machine_roles.isdisjoint(ec2_actions_by_role))

    def test_connector_permissions_match_actual_calls(self):
        profile, = self.resources("AWS::IAM::InstanceProfile").values()
        role_ref, = profile["Properties"]["Roles"]
        policy, = (
            resource["Properties"]["PolicyDocument"]
            for resource in self.resources("AWS::IAM::Policy").values()
            if role_ref in resource["Properties"]["Roles"]
        )
        statements = {statement["Action"]: statement for statement in policy["Statement"]}
        self.assertEqual(set(statements), {
            "states:StartExecution", "states:DescribeExecution", "states:SendTaskSuccess",
        })
        self.assertEqual(statements["states:SendTaskSuccess"]["Resource"], "*")
        resources = statements["states:DescribeExecution"]["Resource"]
        self.assertEqual(len(resources), 2)
        for resource, machine in zip(resources, ("SellerStateMachine", "BuyerStateMachine")):
            parts = resource["Fn::Join"][1]
            self.assertIn({"Fn::GetAtt": [machine, "Name"]}, parts)
            self.assertEqual(parts[-1], ":*")
            self.assertTrue(any(isinstance(part, str) and ":execution:" in part for part in parts))

    def test_describe_scope_allows_only_node_executions(self):
        profile, = self.resources("AWS::IAM::InstanceProfile").values()
        role_ref, = profile["Properties"]["Roles"]
        policy, = (
            resource["Properties"]["PolicyDocument"]
            for resource in self.resources("AWS::IAM::Policy").values()
            if role_ref in resource["Properties"]["Roles"]
        )
        statement, = (entry for entry in policy["Statement"]
                      if entry["Action"] == "states:DescribeExecution")
        refs = {"AWS::Partition": "aws", "AWS::Region": "us-east-1",
                "AWS::AccountId": "123456789012"}
        names = {"SellerStateMachine": "seller-node", "BuyerStateMachine": "buyer-node"}
        patterns = []
        for resource in statement["Resource"]:
            separator, parts = resource["Fn::Join"]
            resolved = []
            for part in parts:
                if isinstance(part, str):
                    resolved.append(part)
                elif "Ref" in part:
                    resolved.append(refs[part["Ref"]])
                else:
                    resolved.append(names[part["Fn::GetAtt"][0]])
            patterns.append(separator.join(resolved))
        cases = {
            "arn:aws:states:us-east-1:123456789012:execution:seller-node:op": True,
            "arn:aws:states:us-east-1:123456789012:execution:buyer-node:op": True,
            "arn:aws:states:us-east-1:123456789012:execution:other:op": False,
            "arn:aws:states:us-east-1:999999999999:execution:seller-node:op": False,
            "arn:aws:states:us-west-2:123456789012:execution:seller-node:op": False,
        }
        for arn, allowed in cases.items():
            self.assertEqual(any(fnmatchcase(arn, pattern) for pattern in patterns), allowed)

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
            "AWS::SecretsManager::Secret": 2,
        }
        types = Counter()
        targets = set()
        for name, output in self.template["Outputs"].items():
            if name == "SagaProxySecurityGroupId":
                target, attribute = output["Value"]["Fn::GetAtt"]
                self.assertEqual(attribute, "GroupId")
                self.assertEqual(
                    self.template["Resources"][target]["Type"],
                    "AWS::EC2::SecurityGroup",
                )
                continue
            self.assertTrue(name.endswith("Arn"))
            value = output["Value"]
            if "Ref" in value:
                target = value["Ref"]
                self.assertIn(
                    target,
                    {
                        **self.resources("AWS::StepFunctions::StateMachineAlias"),
                        **self.resources("AWS::SecretsManager::Secret"),
                    },
                )
            else:
                target, attribute = value["Fn::GetAtt"]
                self.assertEqual(attribute, "Arn")
            self.assertNotIn(target, targets)
            targets.add(target)
            types[self.template["Resources"][target]["Type"]] += 1
        self.assertEqual(types, expected)

    def test_private_network_allows_only_ec2_proxy_ports(self) -> None:
        parameters = self.template["Parameters"]
        self.assertEqual(parameters["ExistingVpcId"]["Type"], "AWS::EC2::VPC::Id")
        self.assertEqual(
            parameters["ExistingSubnetId"]["Type"],
            "AWS::EC2::Subnet::Id",
        )
        self.assertEqual(parameters["DatabaseProxyPort"]["Default"], 15432)
        self.assertEqual(parameters["HttpsProxyPort"]["Default"], 3128)
        self.assertNotIn("ApplicationSecurityGroupId", parameters)

        egress = self.resources("AWS::EC2::SecurityGroupEgress")
        ingress = self.resources("AWS::EC2::SecurityGroupIngress")
        self.assertEqual(len(self.resources("AWS::EC2::SecurityGroup")), 2)
        self.assertEqual(len(egress), 2)
        self.assertEqual(len(ingress), 2)
        expected_ports = {
            json.dumps({"Ref": "DatabaseProxyPort"}, sort_keys=True),
            json.dumps({"Ref": "HttpsProxyPort"}, sort_keys=True),
        }
        for rules, peer_key in (
            (egress, "DestinationSecurityGroupId"),
            (ingress, "SourceSecurityGroupId"),
        ):
            self.assertEqual(
                {
                    json.dumps(rule["Properties"]["FromPort"], sort_keys=True)
                    for rule in rules.values()
                },
                expected_ports,
            )
            for rule in rules.values():
                properties = rule["Properties"]
                self.assertIn(peer_key, properties)
                self.assertNotIn("CidrIp", properties)
                self.assertNotIn("CidrIpv6", properties)

        self.assertFalse(self.resources("AWS::EC2::NatGateway"))
        self.assertFalse(self.resources("AWS::RDS::DBInstance"))
        self.assertFalse(self.resources("AWS::EC2::Instance"))


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
