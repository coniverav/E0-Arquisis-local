"""Validación local de los documentos IAM operativos de E3-055/E3-056."""

from __future__ import annotations

import json
from pathlib import Path
import unittest

from aws_cdk.assertions import Template

from app import build_app


ROOT = Path(__file__).resolve().parents[1]
IAM_DIR = ROOT / "iam"
ACCOUNT = "521294961974"
REGION = "us-east-2"
STACK_ARN = f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/EnergySharkProd-Sagas/*"
BOOTSTRAP_STACK_ARN = (
    f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/CDKToolkit/*"
)
BOOTSTRAP_VERSION_PARAMETER_ARN = (
    f"arn:aws:ssm:{REGION}:{ACCOUNT}:parameter/cdk-bootstrap/hnb659fds/version"
)
PRODUCTION_VPC_ARN = (
    f"arn:aws:ec2:{REGION}:{ACCOUNT}:vpc/vpc-0e7ee77fd065bea62"
)
SECURITY_GROUP_ARN = f"arn:aws:ec2:{REGION}:{ACCOUNT}:security-group/*"
REQUIRED_TAGS = {
    "Project": "EnergyShark",
    "Environment": "prod",
    "ManagedBy": "AWS-CDK",
}

FILES = {
    "bootstrap": "EnergySharkCdkBootstrapOperator-us-east-2.json",
    "execution": "EnergySharkSagasCfnExecution-us-east-2.json",
    "boundary": "EnergySharkCdkBootstrapBoundary-us-east-2.json",
    "caller": "EnergySharkCdkDeployCaller-us-east-2.json",
}

EXPECTED_PUBLISHING_ACTIONS = {
    "ecr:BatchCheckLayerAvailability",
    "ecr:BatchGetImage",
    "ecr:CompleteLayerUpload",
    "ecr:DescribeImages",
    "ecr:DescribeRepositories",
    "ecr:GetAuthorizationToken",
    "ecr:GetDownloadUrlForLayer",
    "ecr:InitiateLayerUpload",
    "ecr:PutImage",
    "ecr:UploadLayerPart",
    "s3:AbortMultipartUpload",
    "s3:DeleteObject",
    "s3:DeleteObjectVersion",
    "s3:GetBucketLocation",
    "s3:GetBucketVersioning",
    "s3:GetEncryptionConfiguration",
    "s3:GetObject",
    "s3:GetObjectVersion",
    "s3:ListBucket",
    "s3:ListBucketMultipartUploads",
    "s3:ListBucketVersions",
    "s3:ListMultipartUploadParts",
    "s3:PutObject",
}


def as_list(value) -> list:
    return value if isinstance(value, list) else [value]


def load(name: str) -> dict:
    return json.loads((IAM_DIR / FILES[name]).read_text(encoding="utf-8"))


def all_actions(policy: dict) -> set[str]:
    return {
        action
        for statement in policy["Statement"]
        for action in as_list(statement["Action"])
    }


class IamPolicyDocumentsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.policies = {name: load(name) for name in FILES}

    def test_documents_are_valid_managed_policy_sized_json(self) -> None:
        self.assertEqual(set(self.policies), set(FILES))
        for name, policy in self.policies.items():
            self.assertEqual(policy["Version"], "2012-10-17", name)
            self.assertTrue(policy["Statement"], name)
            compact = json.dumps(policy, separators=(",", ":"))
            self.assertLessEqual(len(compact), 6144, name)

    def test_no_document_grants_administrator_or_unknown_region(self) -> None:
        for name, policy in self.policies.items():
            rendered = json.dumps(policy)
            self.assertNotIn("AdministratorAccess", rendered, name)
            self.assertNotIn("us-east-1", rendered, name)
            self.assertNotIn('"Action": "*"', rendered, name)
            self.assertNotIn('"Action": ["*"]', rendered, name)

    def test_s3_lifecycle_actions_use_official_iam_names(self) -> None:
        actions = {
            action
            for policy in self.policies.values()
            for action in all_actions(policy)
        }
        self.assertTrue(
            {
                "s3:GetLifecycleConfiguration",
                "s3:PutLifecycleConfiguration",
            }
            <= all_actions(self.policies["bootstrap"])
        )
        self.assertTrue(
            {
                "s3:GetBucketLifecycleConfiguration",
                "s3:PutBucketLifecycleConfiguration",
            }.isdisjoint(actions)
        )

    def test_deploy_caller_assumes_only_required_bootstrap_roles(self) -> None:
        caller = self.policies["caller"]
        statement, = caller["Statement"]
        self.assertEqual(
            set(as_list(statement["Action"])),
            {"sts:AssumeRole", "sts:TagSession"},
        )
        self.assertEqual(len(statement["Resource"]), 3)
        self.assertFalse(any("lookup-role" in arn for arn in statement["Resource"]))
        self.assertTrue(
            all(arn.endswith(f"-{ACCOUNT}-{REGION}") for arn in statement["Resource"])
        )

    def test_cloudformation_write_scope_is_stack_specific(self) -> None:
        write_prefixes = (
            "cloudformation:Create",
            "cloudformation:Delete",
            "cloudformation:Execute",
            "cloudformation:Rollback",
            "cloudformation:Continue",
            "cloudformation:Update",
        )
        expected = {
            "bootstrap": BOOTSTRAP_STACK_ARN,
            "boundary": STACK_ARN,
        }
        for name, resource in expected.items():
            matches = []
            for statement in self.policies[name]["Statement"]:
                actions = as_list(statement["Action"])
                if any(action.startswith(write_prefixes) for action in actions):
                    matches.append(statement)
            self.assertTrue(matches, name)
            self.assertTrue(
                all(statement["Resource"] == resource for statement in matches),
                name,
            )

        self.assertIn(
            "cloudformation:DeleteStack",
            all_actions(self.policies["boundary"]),
        )

    def test_passrole_is_scoped_by_arn_and_destination_service(self) -> None:
        allowed_services = {
            "cloudformation.amazonaws.com",
            "ec2.amazonaws.com",
            "lambda.amazonaws.com",
            "states.amazonaws.com",
        }
        found = set()
        for name in ("execution", "boundary"):
            for statement in self.policies[name]["Statement"]:
                if "iam:PassRole" not in as_list(statement["Action"]):
                    continue
                self.assertNotIn("*", as_list(statement["Resource"]), name)
                services = as_list(
                    statement["Condition"]["StringEquals"][
                        "iam:PassedToService"
                    ]
                )
                found.update(services)
        self.assertEqual(found, allowed_services)

        for name in ("bootstrap", "caller"):
            self.assertNotIn("iam:PassRole", all_actions(self.policies[name]))

    def test_boundary_does_not_block_execution_or_asset_publishing(self) -> None:
        boundary_actions = all_actions(self.policies["boundary"])
        execution_actions = all_actions(self.policies["execution"])
        self.assertTrue(execution_actions <= boundary_actions)
        self.assertTrue(EXPECTED_PUBLISHING_ACTIONS <= boundary_actions)

    def test_cfn_execution_reads_only_exact_bootstrap_version_parameter(self) -> None:
        execution_ssm_statements = [
            statement
            for statement in self.policies["execution"]["Statement"]
            if any(action.startswith("ssm:") for action in as_list(statement["Action"]))
        ]
        self.assertEqual(len(execution_ssm_statements), 1)
        statement, = execution_ssm_statements
        self.assertEqual(as_list(statement["Action"]), ["ssm:GetParameters"])
        self.assertEqual(statement["Resource"], BOOTSTRAP_VERSION_PARAMETER_ARN)

        boundary_matches = [
            statement
            for statement in self.policies["boundary"]["Statement"]
            if statement["Resource"] == BOOTSTRAP_VERSION_PARAMETER_ARN
        ]
        self.assertEqual(len(boundary_matches), 1)
        self.assertTrue(
            {"ssm:GetParameter", "ssm:GetParameters"}
            <= set(as_list(boundary_matches[0]["Action"]))
        )

    def test_execution_policy_covers_services_in_synthesized_sagas_stack(self) -> None:
        _, stacks = build_app(
            {
                "environment": "prod",
                "deploymentScope": "sagas",
                "environments": {
                    "prod": {"stackPrefix": "EnergySharkProd"},
                },
            }
        )
        template = Template.from_stack(stacks.sagas).to_json()
        resource_types = {
            resource["Type"]
            for resource in template["Resources"].values()
            if resource["Type"] != "AWS::CDK::Metadata"
        }
        namespaces_by_resource_type = {
            "AWS::EC2::SecurityGroup": "ec2",
            "AWS::EC2::SecurityGroupEgress": "ec2",
            "AWS::EC2::SecurityGroupIngress": "ec2",
            "AWS::IAM::InstanceProfile": "iam",
            "AWS::IAM::Policy": "iam",
            "AWS::IAM::Role": "iam",
            "AWS::Lambda::Function": "lambda",
            "AWS::Logs::LogGroup": "logs",
            "AWS::SecretsManager::Secret": "secretsmanager",
            "AWS::StepFunctions::StateMachine": "states",
            "AWS::StepFunctions::StateMachineAlias": "states",
            "AWS::StepFunctions::StateMachineVersion": "states",
        }
        self.assertEqual(
            resource_types,
            set(namespaces_by_resource_type),
            "Un recurso nuevo requiere revisar la politica CloudFormation",
        )
        execution_namespaces = {
            action.split(":", 1)[0]
            for action in all_actions(self.policies["execution"])
        }
        self.assertTrue(
            set(namespaces_by_resource_type.values()) <= execution_namespaces
        )
        self.assertEqual(
            sum(
                resource["Type"] == "AWS::Lambda::Function"
                for resource in template["Resources"].values()
            ),
            13,
        )

        tag_restricted_types = {
            "AWS::EC2::SecurityGroup",
            "AWS::IAM::Role",
            "AWS::Lambda::Function",
            "AWS::Logs::LogGroup",
            "AWS::SecretsManager::Secret",
            "AWS::StepFunctions::StateMachine",
        }
        for logical_id, resource in template["Resources"].items():
            if resource["Type"] not in tag_restricted_types:
                continue
            actual_tags = {
                tag["Key"]: tag["Value"]
                for tag in resource.get("Properties", {}).get("Tags", [])
            }
            self.assertEqual(actual_tags, REQUIRED_TAGS, logical_id)

        secrets = {
            resource["Properties"]["Name"]: resource
            for resource in template["Resources"].values()
            if resource["Type"] == "AWS::SecretsManager::Secret"
        }
        self.assertEqual(
            set(secrets),
            {
                "EnergySharkProd-Sagas-Database",
                "EnergySharkProd-Sagas-Wallet",
            },
        )
        for resource in secrets.values():
            self.assertEqual(resource["DeletionPolicy"], "Retain")
            self.assertEqual(resource["UpdateReplacePolicy"], "Retain")

    def test_execution_policy_covers_resource_lifecycle_and_rollback(self) -> None:
        required_by_resource = {
            "AWS::EC2::SecurityGroup": {
                "ec2:CreateSecurityGroup",
                "ec2:CreateTags",
                "ec2:DeleteSecurityGroup",
                "ec2:DeleteTags",
                "ec2:DescribeSecurityGroups",
                "ec2:RevokeSecurityGroupEgress",
            },
            "AWS::EC2::SecurityGroupEgress": {
                "ec2:AuthorizeSecurityGroupEgress",
                "ec2:RevokeSecurityGroupEgress",
            },
            "AWS::EC2::SecurityGroupIngress": {
                "ec2:AuthorizeSecurityGroupIngress",
                "ec2:RevokeSecurityGroupIngress",
            },
            "AWS::IAM::InstanceProfile": {
                "iam:AddRoleToInstanceProfile",
                "iam:CreateInstanceProfile",
                "iam:DeleteInstanceProfile",
                "iam:GetInstanceProfile",
                "iam:RemoveRoleFromInstanceProfile",
            },
            "AWS::IAM::Policy": {
                "iam:DeleteRolePolicy",
                "iam:GetRolePolicy",
                "iam:PutRolePolicy",
            },
            "AWS::IAM::Role": {
                "iam:CreateRole",
                "iam:DeleteRole",
                "iam:GetRole",
                "iam:PassRole",
                "iam:TagRole",
                "iam:UntagRole",
                "iam:UpdateAssumeRolePolicy",
            },
            "AWS::Lambda::Function": {
                "lambda:CreateFunction",
                "lambda:DeleteFunction",
                "lambda:GetFunction",
                "lambda:GetFunctionConfiguration",
                "lambda:ListTags",
                "lambda:TagResource",
                "lambda:UntagResource",
                "lambda:UpdateFunctionCode",
                "lambda:UpdateFunctionConfiguration",
            },
            "AWS::Logs::LogGroup": {
                "logs:CreateLogGroup",
                "logs:DeleteLogGroup",
                "logs:PutRetentionPolicy",
                "logs:TagResource",
                "logs:UntagResource",
            },
            "AWS::SecretsManager::Secret": {
                "secretsmanager:CreateSecret",
                "secretsmanager:DeleteSecret",
                "secretsmanager:DescribeSecret",
                "secretsmanager:GetRandomPassword",
                "secretsmanager:TagResource",
                "secretsmanager:UntagResource",
                "secretsmanager:UpdateSecret",
            },
            "AWS::StepFunctions::StateMachine": {
                "states:CreateStateMachine",
                "states:DeleteStateMachine",
                "states:DescribeStateMachine",
                "states:TagResource",
                "states:UntagResource",
                "states:UpdateStateMachine",
            },
            "AWS::StepFunctions::StateMachineAlias": {
                "states:CreateStateMachineAlias",
                "states:DeleteStateMachineAlias",
                "states:DescribeStateMachineAlias",
                "states:UpdateStateMachineAlias",
            },
            "AWS::StepFunctions::StateMachineVersion": {
                "states:DeleteStateMachineVersion",
                "states:PublishStateMachineVersion",
            },
        }
        _, stacks = build_app(
            {
                "environment": "prod",
                "deploymentScope": "sagas",
                "environments": {"prod": {"stackPrefix": "EnergySharkProd"}},
            }
        )
        template = Template.from_stack(stacks.sagas).to_json()
        present_types = {
            resource["Type"] for resource in template["Resources"].values()
        }
        execution_actions = all_actions(self.policies["execution"])
        for resource_type, required_actions in required_by_resource.items():
            self.assertIn(resource_type, present_types)
            self.assertTrue(
                required_actions <= execution_actions,
                f"{resource_type}: {required_actions - execution_actions}",
            )

    def test_security_group_permissions_are_limited_to_production_vpc(self) -> None:
        execution = self.policies["execution"]["Statement"]
        boundary = self.policies["boundary"]["Statement"]

        execution_create = [
            statement
            for statement in execution
            if as_list(statement["Action"]) == ["ec2:CreateSecurityGroup"]
        ]
        self.assertEqual(len(execution_create), 2)
        self.assertEqual(
            {statement["Resource"] for statement in execution_create},
            {PRODUCTION_VPC_ARN, SECURITY_GROUP_ARN},
        )
        self.assertTrue(
            all("Condition" not in statement for statement in execution_create)
        )

        create_tags, = (
            statement
            for statement in execution
            if as_list(statement["Action"]) == ["ec2:CreateTags"]
        )
        self.assertEqual(create_tags["Resource"], SECURITY_GROUP_ARN)
        self.assertEqual(
            create_tags["Condition"]["StringEqualsIfExists"]["ec2:CreateAction"],
            "CreateSecurityGroup",
        )
        self.assertEqual(
            create_tags["Condition"]["ArnEquals"]["ec2:Vpc"],
            PRODUCTION_VPC_ARN,
        )
        for key, value in REQUIRED_TAGS.items():
            self.assertEqual(
                create_tags["Condition"]["StringEquals"][f"aws:RequestTag/{key}"],
                value,
            )

        mutation, = (
            statement
            for statement in execution
            if "ec2:DeleteSecurityGroup" in as_list(statement["Action"])
        )
        self.assertEqual(mutation["Resource"], SECURITY_GROUP_ARN)
        self.assertEqual(
            mutation["Condition"]["ArnEquals"]["ec2:Vpc"],
            PRODUCTION_VPC_ARN,
        )
        for key, value in REQUIRED_TAGS.items():
            self.assertEqual(
                mutation["Condition"]["StringEquals"][f"aws:ResourceTag/{key}"],
                value,
            )

        boundary_create, = (
            statement
            for statement in boundary
            if as_list(statement["Action"]) == ["ec2:CreateSecurityGroup"]
        )
        self.assertEqual(
            set(boundary_create["Resource"]),
            {PRODUCTION_VPC_ARN, SECURITY_GROUP_ARN},
        )
        boundary_mutation, = (
            statement
            for statement in boundary
            if "ec2:DeleteSecurityGroup" in as_list(statement["Action"])
        )
        self.assertEqual(boundary_mutation["Resource"], SECURITY_GROUP_ARN)
        self.assertEqual(
            boundary_mutation["Condition"]["ArnEquals"]["ec2:Vpc"],
            PRODUCTION_VPC_ARN,
        )
        self.assertEqual(
            boundary_mutation["Condition"]["StringEqualsIfExists"][
                "ec2:CreateAction"
            ],
            "CreateSecurityGroup",
        )

    def test_boundary_does_not_allow_other_stack_or_bootstrap_mutation(self) -> None:
        rendered = json.dumps(self.policies["boundary"])
        self.assertNotIn("ExistingInfrastructure", rendered)
        self.assertNotIn("Monitoring", rendered)
        self.assertNotIn("stack/*", rendered)
        self.assertNotIn("role/cdk-hnb659fds-*", rendered)

    def test_wildcard_resources_are_documented_exceptions(self) -> None:
        allowed = {
            "cloudformation:DescribeAccountLimits",
            "cloudformation:GetTemplateSummary",
            "cloudformation:ListStacks",
            "cloudformation:ValidateTemplate",
            "ec2:DescribeSecurityGroups",
            "ec2:DescribeSubnets",
            "ec2:DescribeVpcs",
            "ecr:GetAuthorizationToken",
            "logs:DescribeLogGroups",
            "secretsmanager:GetRandomPassword",
            "sts:GetCallerIdentity",
        }
        for name, policy in self.policies.items():
            for statement in policy["Statement"]:
                if statement["Resource"] != "*":
                    continue
                self.assertTrue(set(as_list(statement["Action"])) <= allowed, name)


if __name__ == "__main__":
    unittest.main()
