"""E3-077: contratos del auditor de políticas IAM."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "iac_policy_audit.py"
spec = importlib.util.spec_from_file_location("iac_policy_audit", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


TEMPLATE = {
    "Resources": {
        "HostRole": {
            "Type": "AWS::IAM::Role",
            "Properties": {
                "AssumeRolePolicyDocument": {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {"Service": "ec2.amazonaws.com"},
                            "Action": "sts:AssumeRole",
                        }
                    ],
                },
                "Policies": [
                    {
                        "PolicyName": "inline",
                        "PolicyDocument": {
                            "Version": "2012-10-17",
                            "Statement": [
                                {
                                    "Effect": "Allow",
                                    "Action": "logs:PutLogEvents",
                                    "Resource": {
                                        "Fn::GetAtt": ["HostLogGroup", "Arn"]
                                    },
                                }
                            ],
                        },
                    }
                ],
            },
        },
        "StandalonePolicy": {
            "Type": "AWS::IAM::Policy",
            "Properties": {
                "PolicyName": "standalone",
                "PolicyDocument": {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": "cloudwatch:PutMetricData",
                            "Resource": "*",
                        }
                    ],
                },
            },
        },
    }
}


class PlaceholderTest(unittest.TestCase):
    def test_get_att_becomes_log_group_arn(self) -> None:
        resolved = module.resolve_placeholders(
            {"Fn::GetAtt": ["HostLogGroup", "Arn"]}
        )
        self.assertEqual(
            resolved,
            "arn:aws:logs:us-east-2:111122223333:log-group:/placeholder/HostLogGroup",
        )

    def test_ref_partition_and_account(self) -> None:
        self.assertEqual(
            module.resolve_placeholders({"Ref": "AWS::Partition"}), "aws"
        )
        self.assertEqual(
            module.resolve_placeholders({"Ref": "AWS::AccountId"}),
            "111122223333",
        )

    def test_ref_parameter_becomes_service_arn(self) -> None:
        secret = module.resolve_placeholders({"Ref": "WalletSecretArn"})
        self.assertTrue(secret.startswith("arn:aws:secretsmanager:"), secret)

        alias = module.resolve_placeholders({"Ref": "SellerStateMachineAlias"})
        self.assertTrue(alias.startswith("arn:aws:states:"), alias)
        self.assertTrue(alias.endswith(":live"), alias)

    def test_sub_replaces_pseudo_parameters(self) -> None:
        resolved = module.resolve_placeholders(
            {"Fn::Sub": "arn:${AWS::Partition}:s3:::bucket-${AWS::Region}"}
        )
        self.assertEqual(resolved, "arn:aws:s3:::bucket-us-east-2")

    def test_region_is_configurable(self) -> None:
        resolved = module.resolve_placeholders(
            {"Fn::GetAtt": ["LogGroup", "Arn"]}, region="eu-west-1"
        )
        self.assertIn("eu-west-1", resolved)

    def test_leaves_plain_values_untouched(self) -> None:
        statement = {"Effect": "Allow", "Action": "s3:GetObject", "Resource": "*"}
        self.assertEqual(module.resolve_placeholders(statement), statement)


class ExtractionTest(unittest.TestCase):
    def test_extracts_inline_trust_and_standalone_policies(self) -> None:
        extracted = module.extract_policies(TEMPLATE)
        self.assertEqual(
            [origin for origin, _, _ in extracted],
            [
                "HostRole/inline[0]",
                "StandalonePolicy",
            ],
        )
        self.assertEqual(
            [policy_type for _, _, policy_type in extracted],
            [module.IDENTITY, module.IDENTITY],
        )

    def test_reads_template_from_disk(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "template.json"
            path.write_text(json.dumps(TEMPLATE), encoding="utf-8")
            template = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(len(module.extract_policies(template)), 2)

    def test_empty_template_has_no_policies(self) -> None:
        self.assertEqual(module.extract_policies({"Resources": {}}), [])


if __name__ == "__main__":
    unittest.main()
