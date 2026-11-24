"""E3-077: permisos mínimos y secretos fuera del template.

Verifica que ningún rol de la infraestructura declare acciones comodín, que
los `Resource: "*"` estén justificados por una API que no admite ARN, y que
los secretos lleguen a las Lambdas solo como referencia a un parámetro
`NoEcho`.
"""

import json
import re
import unittest

from aws_cdk.assertions import Template

from app import build_app


CONTEXT = {
    "environment": "dev",
    "environments": {"dev": {"stackPrefix": "EnergySharkDev"}},
}

# Acciones que AWS solo acepta con Resource "*" o que no admiten ARN.
# Cada entrada está justificada en docs/E3-077-permisos-minimos-secretos-iac.md.
WILDCARD_ALLOWED_ACTIONS = {
    "cloudwatch:PutMetricData",
    "ec2:DescribeInstances",
    "ec2:DescribeTags",
    "ec2:DescribeVolumes",
    "states:SendTaskSuccess",
    "states:SendTaskFailure",
    "logs:CreateLogDelivery",
    "logs:GetLogDelivery",
    "logs:UpdateLogDelivery",
    "logs:DeleteLogDelivery",
    "logs:ListLogDeliveries",
    "logs:PutResourcePolicy",
    "logs:DescribeResourcePolicies",
    "logs:DescribeLogGroups",
}

SECRET_VALUE_PATTERN = re.compile(
    r"(AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY|"
    r"gh[pousr]_[A-Za-z0-9]{20,})"
)


def as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def no_echo_parameters(template: dict) -> set[str]:
    return {
        name
        for name, parameter in template.get("Parameters", {}).items()
        if parameter.get("NoEcho") is True
    }


def policies_of(template: dict):
    for resource in template["Resources"].values():
        if resource["Type"] == "AWS::IAM::Policy":
            yield resource["Properties"]["PolicyDocument"]
        if resource["Type"] == "AWS::IAM::Role":
            for policy in as_list(resource["Properties"].get("Policies")):
                yield policy["PolicyDocument"]


class LeastPrivilegeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app, self.stacks = build_app(CONTEXT)
        self.existing = Template.from_stack(self.stacks.existing).to_json()
        self.monitoring = Template.from_stack(self.stacks.monitoring).to_json()
        self.sagas = Template.from_stack(self.stacks.sagas).to_json()

    def resources_of(self, template, resource_type):
        return {
            logical_id: resource
            for logical_id, resource in template["Resources"].items()
            if resource["Type"] == resource_type
        }

    def test_no_role_grants_wildcard_actions(self) -> None:
        for name, template in (
            ("existing", self.existing),
            ("monitoring", self.monitoring),
            ("sagas", self.sagas),
        ):
            for document in policies_of(template):
                for statement in document["Statement"]:
                    actions = as_list(statement.get("Action"))
                    self.assertNotIn(
                        "*",
                        actions,
                        f"{name} concede acciones comodín: {statement}",
                    )
                    self.assertNotIn(
                        "iam:*",
                        actions,
                        f"{name} concede administración de IAM",
                    )

    def test_wildcard_resources_are_justified(self) -> None:
        unjustified = []
        for name, template in (
            ("existing", self.existing),
            ("monitoring", self.monitoring),
            ("sagas", self.sagas),
        ):
            for document in policies_of(template):
                for statement in document["Statement"]:
                    if "*" not in as_list(statement.get("Resource")):
                        continue
                    actions = set(as_list(statement.get("Action")))
                    if actions <= WILDCARD_ALLOWED_ACTIONS:
                        continue
                    unjustified.append(
                        (
                            name,
                            statement.get("Sid"),
                            sorted(actions - WILDCARD_ALLOWED_ACTIONS),
                        )
                    )
        self.assertEqual(unjustified, [])

    def test_instance_role_uses_one_managed_policy(self) -> None:
        roles = self.resources_of(self.existing, "AWS::IAM::Role")
        instance_roles = [
            role
            for role in roles.values()
            if "ec2.amazonaws.com"
            in json.dumps(role["Properties"]["AssumeRolePolicyDocument"])
        ]
        self.assertEqual(len(instance_roles), 1)
        managed = json.dumps(instance_roles[0]["Properties"]["ManagedPolicyArns"])
        self.assertIn("AmazonSSMManagedInstanceCore", managed)
        self.assertNotIn("CloudWatchAgentServerPolicy", managed)
        self.assertNotIn("AdministratorAccess", managed)
        self.assertNotIn("PowerUserAccess", managed)

    def test_metric_publication_is_scoped_by_namespace(self) -> None:
        documents = list(policies_of(self.existing))
        statements = [
            statement
            for document in documents
            for statement in document["Statement"]
            if "cloudwatch:PutMetricData" in as_list(statement.get("Action"))
        ]
        self.assertEqual(len(statements), 1)
        self.assertEqual(
            statements[0]["Condition"],
            {"StringEquals": {"cloudwatch:namespace": "EnergyShark/Host"}},
        )

    def test_log_writes_are_scoped_to_the_host_log_group(self) -> None:
        statements = [
            statement
            for document in policies_of(self.existing)
            for statement in document["Statement"]
            if "logs:PutLogEvents" in as_list(statement.get("Action"))
        ]
        self.assertTrue(statements)
        for statement in statements:
            resources = json.dumps(statement["Resource"])
            self.assertIn("HostLogGroup", resources)
            self.assertNotEqual(resources, '"*"')
            self.assertNotIn("logs:CreateLogGroup", as_list(statement["Action"]))

    def test_secret_parameters_are_no_echo(self) -> None:
        secrets = no_echo_parameters(self.sagas)
        self.assertGreaterEqual(len(secrets), 3)
        for name in secrets:
            description = self.sagas["Parameters"][name]["Description"]
            self.assertIn("ARN", description)
            self.assertIn("Secrets Manager", description)

    def test_lambda_receives_secret_arns_not_values(self) -> None:
        secret_parameters = no_echo_parameters(self.sagas)
        self.assertTrue(secret_parameters)

        checked = 0
        for function in self.resources_of(
            self.sagas, "AWS::Lambda::Function"
        ).values():
            for key, value in (
                function["Properties"].get("Environment", {})
                .get("Variables", {})
                .items()
            ):
                if not key.endswith("_SECRET_ARN"):
                    continue
                checked += 1
                self.assertIn(
                    value.get("Ref"),
                    secret_parameters,
                    f"{key} no referencia un parámetro NoEcho",
                )
        self.assertGreater(checked, 0)

    def test_templates_do_not_embed_credentials(self) -> None:
        for name, template in (
            ("existing", self.existing),
            ("monitoring", self.monitoring),
            ("sagas", self.sagas),
        ):
            serialized = json.dumps(template)
            self.assertIsNone(
                SECRET_VALUE_PATTERN.search(serialized),
                f"{name} contiene una credencial embebida",
            )

    def test_no_policy_grants_full_administrator(self) -> None:
        forbidden = {
            "Action": ["*"],
            "Resource": "*",
        }
        for template in (self.existing, self.monitoring, self.sagas):
            for document in policies_of(template):
                for statement in document["Statement"]:
                    self.assertNotEqual(
                        {
                            "Action": as_list(statement.get("Action")),
                            "Resource": as_list(statement.get("Resource")),
                        },
                        {
                            "Action": forbidden["Action"],
                            "Resource": [forbidden["Resource"]],
                        },
                    )


if __name__ == "__main__":
    unittest.main()
