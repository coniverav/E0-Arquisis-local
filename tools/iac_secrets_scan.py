#!/usr/bin/env python3
"""E3-077: busca secretos y credenciales dentro del repositorio.

Revisa archivos de texto del árbol de trabajo y reporta:

- claves de acceso de AWS (`AKIA`/`ASIA`),
- bloques de llave privada PEM,
- tokens de GitHub,
- asignaciones de secretos de Auth0,
- archivos `.env` y llaves (`*.pem`, `*.key`, `*.p12`) versionables.

No lee credenciales de AWS ni contacta servicios. Solo inspecciona archivos.

Uso:
    python3 tools/iac_secrets_scan.py
    python3 tools/iac_secrets_scan.py infra docs
"""

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

SKIP_DIRECTORIES = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "cdk.out",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    "dist",
    "build",
}

SKIP_SUFFIXES = {".zip", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".whl"}

SAFE_ENV_SUFFIXES = (".example", ".sample", ".template", ".dist")

PATTERNS = (
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    (
        "aws_secret_key",
        re.compile(
            r"(?i)aws_?secret_?access_?key\s*[=:]\s*['\"]?[A-Za-z0-9/+=]{40}\b"
        ),
    ),
    (
        "private_key",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    (
        "auth0_secret",
        re.compile(
            r"(?i)auth0[_-]?(?:client[_-]?)?secret\s*[=:]\s*['\"]?[A-Za-z0-9_\-\.]{16,}"
        ),
    ),
)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    kind: str
    preview: str


def _is_sensitive_filename(name: str) -> str | None:
    lowered = name.lower()
    if lowered == ".env" or lowered.startswith(".env."):
        if not lowered.endswith(SAFE_ENV_SUFFIXES):
            return "env_file"
        return None
    if lowered.endswith((".pem", ".key", ".p12", ".pfx")):
        return "key_file"
    return None


def _mask(value: str) -> str:
    value = value.strip()
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}***{value[-2:]}"


def scan_file(path: Path, root: Path) -> list[Finding]:
    relative = str(path.relative_to(root))
    findings: list[Finding] = []

    kind = _is_sensitive_filename(path.name)
    if kind:
        findings.append(Finding(relative, 0, kind, path.name))

    try:
        text = path.read_text(encoding="utf-8", errors="strict")
    except (UnicodeDecodeError, OSError):
        return findings

    for number, line in enumerate(text.splitlines(), start=1):
        if len(line) > 4000:
            line = line[:4000]
        for name, pattern in PATTERNS:
            match = pattern.search(line)
            if match:
                findings.append(
                    Finding(relative, number, name, _mask(match.group(0)))
                )
    return findings


def iter_files(targets: list[Path], root: Path):
    for target in targets:
        if target.is_file():
            yield target
            continue
        for path in target.rglob("*"):
            if not path.is_file():
                continue
            if any(part in SKIP_DIRECTORIES for part in path.parts):
                continue
            if path.suffix.lower() in SKIP_SUFFIXES:
                continue
            yield path


def scan(targets: list[Path], root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in iter_files(targets, root):
        findings.extend(scan_file(path, root))
    return sorted(findings, key=lambda item: (item.path, item.line, item.kind))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="*",
        help="Archivos o carpetas a revisar; por defecto el repositorio",
    )
    parser.add_argument(
        "--root",
        default=None,
        help="Raíz para rutas relativas; por defecto el directorio actual",
    )
    args = parser.parse_args(argv)

    root = Path(args.root).resolve() if args.root else Path.cwd().resolve()
    targets = [Path(item).resolve() for item in args.paths] or [root]

    findings = scan(targets, root)
    if not findings:
        print(f"Sin secretos detectados en {len(targets)} ruta(s).")
        return 0

    print(f"Hallazgos: {len(findings)}")
    for finding in findings:
        location = finding.path if finding.line == 0 else f"{finding.path}:{finding.line}"
        print(f"{location}: {finding.kind} {finding.preview}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
