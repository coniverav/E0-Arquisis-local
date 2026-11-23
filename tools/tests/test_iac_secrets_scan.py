"""E3-077: contratos del escáner de secretos del repositorio."""

import importlib.util
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "iac_secrets_scan.py"

# Las muestras se arman en tiempo de ejecución para que este archivo no
# contenga los literales que el escáner debe detectar.
AWS_KEY = "AKIA" + "IOSFODNN7EXAMPLE"
PEM_BLOCK = "-----BEGIN RSA " + "PRIVATE " + "KEY-----"
GITHUB_TOKEN = "gho_" + "0123456789abcdefghijKLMNOP"
AUTH0_VALUE = "abcdef" + "0123456789abcdef"
spec = importlib.util.spec_from_file_location("iac_secrets_scan", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ScannerTest(unittest.TestCase):
    def scan_tree(self, files: dict[str, str]):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, content in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            return module.scan([root], root), root

    def test_detects_aws_access_key(self) -> None:
        findings, _ = self.scan_tree(
            {"config.py": f'KEY = "{AWS_KEY}"\n'}
        )
        self.assertEqual([finding.kind for finding in findings], ["aws_access_key"])
        self.assertEqual(findings[0].line, 1)
        self.assertNotIn(AWS_KEY, findings[0].preview)

    def test_detects_private_key_block(self) -> None:
        findings, _ = self.scan_tree(
            {"id_rsa": PEM_BLOCK + "\nabc\n"}
        )
        self.assertIn("private_key", {finding.kind for finding in findings})

    def test_flags_dotenv_file(self) -> None:
        findings, _ = self.scan_tree({".env": "DB_PASSWORD=supersecret\n"})
        self.assertIn("env_file", {finding.kind for finding in findings})

    def test_ignores_example_env_file(self) -> None:
        findings, _ = self.scan_tree(
            {".env.example": "DB_PASSWORD=cambiar\n"}
        )
        self.assertEqual(findings, [])

    def test_detects_github_and_auth0_tokens(self) -> None:
        findings, _ = self.scan_tree(
            {
                "ci.yml": (
                    f"token: {GITHUB_TOKEN}\n"
                    f"AUTH0_CLIENT_SECRET={AUTH0_VALUE}\n"
                )
            }
        )
        kinds = {finding.kind for finding in findings}
        self.assertIn("github_token", kinds)
        self.assertIn("auth0_secret", kinds)

    def test_skips_binary_and_build_directories(self) -> None:
        findings, _ = self.scan_tree(
            {
                "cdk.out/template.json": AWS_KEY,
                "images/logo.png": AWS_KEY,
                "docs/nota.md": "sin credenciales\n",
            }
        )
        self.assertEqual(findings, [])

    def test_main_returns_zero_when_clean(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "README.md").write_text("hola\n", encoding="utf-8")
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = module.main(["--root", str(root)])
        self.assertEqual(code, 0)
        self.assertIn("Sin secretos detectados", buffer.getvalue())

    def test_main_returns_one_when_found(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "leak.txt").write_text(
                AWS_KEY + "\n", encoding="utf-8"
            )
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = module.main(["--root", str(root)])
        self.assertEqual(code, 1)
        self.assertIn("aws_access_key", buffer.getvalue())

    def test_repository_has_no_secrets(self) -> None:
        repository = MODULE_PATH.parents[1]
        findings = module.scan([repository], repository)
        self.assertEqual(findings, [])


if __name__ == "__main__":
    unittest.main()
