"""E3-073: contratos del saneamiento de la política S3."""
import importlib.util
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "prowler_s3_policy.py"
spec = importlib.util.spec_from_file_location("prowler_s3_policy", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "PublicReadGetObject",
            "Effect": "Allow",
            "Principal": "*",
            "Action": "s3:GetObject",
            "Resource": "arn:aws:s3:::energyshark-g10-frontend/*",
        },
        {
            "Sid": "AllowCloudFrontServicePrincipal",
            "Effect": "Allow",
            "Principal": {"Service": "cloudfront.amazonaws.com"},
            "Action": "s3:GetObject",
            "Resource": "arn:aws:s3:::energyshark-g10-frontend/*",
            "Condition": {
                "ArnLike": {
                    "AWS:SourceArn": (
                        "arn:aws:cloudfront::521294961974:distribution/EFUBNHG9ZB6SV"
                    )
                }
            },
        },
    ],
}


class SanitizeTests(unittest.TestCase):
    def test_removes_public_allow_and_keeps_cloudfront(self):
        result = module.sanitize_policy(POLICY)
        sids = [statement["Sid"] for statement in result["Statement"]]
        self.assertNotIn("PublicReadGetObject", sids)
        self.assertIn("AllowCloudFrontServicePrincipal", sids)
        self.assertIn("DenyInsecureTransport", sids)

    def test_deny_uses_bucket_arn_and_condition(self):
        result = module.sanitize_policy(POLICY)
        deny = next(
            statement
            for statement in result["Statement"]
            if statement["Sid"] == "DenyInsecureTransport"
        )
        self.assertEqual(
            deny["Resource"],
            [
                "arn:aws:s3:::energyshark-g10-frontend",
                "arn:aws:s3:::energyshark-g10-frontend/*",
            ],
        )
        self.assertEqual(deny["Condition"], {"Bool": {"aws:SecureTransport": "false"}})

    def test_is_idempotent(self):
        once = module.sanitize_policy(POLICY)
        twice = module.sanitize_policy(once)
        self.assertEqual(once, twice)

    def test_keeps_non_public_account_principals(self):
        policy = {
            "Statement": [
                {
                    "Sid": "AllowAccount",
                    "Effect": "Allow",
                    "Principal": {"AWS": "arn:aws:iam::521294961974:root"},
                    "Action": "s3:GetObject",
                    "Resource": "arn:aws:s3:::bucket/*",
                }
            ]
        }
        result = module.sanitize_policy(policy)
        sids = [statement["Sid"] for statement in result["Statement"]]
        self.assertIn("AllowAccount", sids)

    def test_rejects_policy_without_bucket_arn(self):
        with self.assertRaises(ValueError):
            module.sanitize_policy({"Statement": []})


if __name__ == "__main__":
    unittest.main()
