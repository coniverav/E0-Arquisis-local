"""E3-071/E3-075: contratos del triage y de la comparación Prowler."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "prowler_report.py"
spec = importlib.util.spec_from_file_location("prowler_report", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


OCSF_FINDINGS = [
    {
        "finding_info": {
            "uid": "s3_bucket_level_public_access_block",
            "title": "S3 bucket does not have public access block",
        },
        "severity_id": 4,
        "status_code": "FAIL",
        "resources": [{"uid": "arn:aws:s3:::energyshark-g10-frontend"}],
    },
    {
        "finding_info": {
            "uid": "ec2_ebs_default_encryption",
            "title": "EBS default encryption is disabled",
        },
        "severity_id": 3,
        "status_code": "FAIL",
        "resources": [{"uid": "us-east-1"}],
    },
    {
        "finding_info": {
            "uid": "iam_user_mfa_enabled_console_access",
            "title": "IAM user has no MFA",
        },
        "severity_id": 5,
        "status_code": "FAIL",
        "resources": [{"uid": "grupo10"}],
    },
    {
        "finding_info": {"uid": "s3_bucket_versioning", "title": "Versioning enabled"},
        "severity_id": 2,
        "status_code": "PASS",
        "resources": [{"uid": "arn:aws:s3:::energyshark-g10-frontend"}],
    },
]

CSV_TEXT = "\n".join(
    [
        "CHECK_ID;CHECK_TITLE;STATUS;SEVERITY;RESOURCE_UID",
        "ec2_ebs_default_encryption;Cifrado EBS;FAIL;medium;us-east-1",
        "s3_bucket_level_public_access_block;S3 público;PASS;high;arn:aws:s3:::bucket",
    ]
)


class LoaderTests(unittest.TestCase):
    def write(self, directory, name, content):
        path = Path(directory) / name
        path.write_text(content, encoding="utf-8")
        return path

    def test_loads_ocsf_json_findings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(directory, "baseline.ocsf.json", json.dumps({"findings": OCSF_FINDINGS}))
            findings = module.load_findings(path)
        self.assertEqual(len(findings), 4)
        self.assertEqual(findings[0]["check_id"], "s3_bucket_level_public_access_block")
        self.assertEqual(findings[0]["severity"], "high")
        self.assertEqual(findings[0]["resource"], "arn:aws:s3:::energyshark-g10-frontend")

    def test_loads_csv_with_semicolons(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(directory, "baseline.csv", CSV_TEXT)
            findings = module.load_findings(path)
        self.assertEqual(len(findings), 2)
        self.assertEqual(findings[0]["severity"], "medium")
        self.assertEqual(findings[1]["status"], "PASS")

    def test_scans_directory_and_ignores_unreadable_json(self):
        with tempfile.TemporaryDirectory() as directory:
            self.write(directory, "output.json", json.dumps({"findings": OCSF_FINDINGS}))
            self.write(directory, "compliance.json", "{not json")
            findings = module.load_findings(directory)
        self.assertEqual(len(findings), 4)


class TriageTests(unittest.TestCase):
    def test_triage_lists_only_medium_plus_failures(self):
        findings = module._load_json(json.dumps({"findings": OCSF_FINDINGS}), "baseline")
        report = module.render_triage(findings)
        self.assertIn("MEDIUM+: 3", report)
        self.assertIn("`iam_user_mfa_enabled_console_access`", report)
        self.assertIn("`s3_bucket_level_public_access_block`", report)
        self.assertIn("`ec2_ebs_default_encryption`", report)
        self.assertNotIn("s3_bucket_versioning", report)
        self.assertLess(report.index("iam_user_mfa"), report.index("ec2_ebs"))

    def test_triage_matches_documented_candidates(self):
        findings = module._load_json(json.dumps({"findings": OCSF_FINDINGS}), "baseline")
        report = module.render_triage(findings)
        self.assertIn("## Candidatos de docs/E3-011", report)
        self.assertIn("MFA de usuario IAM con consola", report)
        self.assertIn("Bloqueo de acceso público S3", report)


class CompareTests(unittest.TestCase):
    def test_compare_marks_fixed_and_regressed_checks(self):
        before = module._load_json(json.dumps({"findings": OCSF_FINDINGS}), "before")
        after_payload = [
            {
                "finding_info": {"uid": "s3_bucket_level_public_access_block"},
                "severity_id": 4,
                "status_code": "PASS",
                "resources": [{"uid": "arn:aws:s3:::energyshark-g10-frontend"}],
            },
            {
                "finding_info": {"uid": "ec2_ebs_default_encryption"},
                "severity_id": 3,
                "status_code": "PASS",
                "resources": [{"uid": "us-east-1"}],
            },
            {
                "finding_info": {"uid": "iam_user_mfa_enabled_console_access"},
                "severity_id": 5,
                "status_code": "FAIL",
                "resources": [{"uid": "grupo10"}],
            },
            {
                "finding_info": {"uid": "s3_bucket_versioning"},
                "severity_id": 2,
                "status_code": "FAIL",
                "resources": [{"uid": "arn:aws:s3:::energyshark-g10-frontend"}],
            },
        ]
        after = module._load_json(json.dumps(after_payload), "after")
        report = module.render_compare(before, after)
        self.assertIn("corregidos: 2", report)
        self.assertIn("regresiones: 1", report)
        self.assertIn("| `s3_bucket_level_public_access_block` | high | 1 | 0 | corregido |", report)
        self.assertIn("| `s3_bucket_versioning` | low | 0 | 1 | regresión |", report)
        self.assertNotIn("sin cambio", report)


class RealOcsfTests(unittest.TestCase):
    """Formato real de Prowler 5.50: `status` es ciclo de vida, no resultado."""

    def record(self, status_code):
        return {
            "finding_info": {
                "analytic": {
                    "uid": "ec2_ebs_default_encryption",
                    "name": "EBS default encryption is enabled",
                },
                "uid": (
                    "prowler-aws-ec2_ebs_default_encryption-"
                    "521294961974-us-east-2-521294961974"
                ),
            },
            "severity": "High",
            "severity_id": 4,
            "status": "New",
            "status_code": status_code,
            "status_detail": "detalle",
            "resources": [{"uid": "arn:aws:ec2:us-east-2:521294961974:volume"}],
        }

    def test_uses_status_code_as_result(self):
        findings = module._load_json(json.dumps([self.record("PASS")]), "after")
        self.assertEqual(findings[0]["status"], "PASS")
        self.assertFalse(module._is_failed(findings[0]["status"]))

    def test_uses_analytic_uid_as_check_id(self):
        findings = module._load_json(json.dumps([self.record("FAIL")]), "before")
        self.assertEqual(findings[0]["check_id"], "ec2_ebs_default_encryption")
        self.assertEqual(findings[0]["severity"], "high")
        self.assertTrue(module._is_failed(findings[0]["status"]))

    def test_compare_detects_correction_in_real_format(self):
        before = module._load_json(json.dumps([self.record("FAIL")]), "before")
        after = module._load_json(json.dumps([self.record("PASS")]), "after")
        report = module.render_compare(before, after)
        self.assertIn("corregidos: 1", report)
        self.assertIn("| `ec2_ebs_default_encryption` | high | 1 | 0 | corregido |", report)


if __name__ == "__main__":
    unittest.main()
