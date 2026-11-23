#!/usr/bin/env python3
"""E3-077: audita las políticas IAM del template con IAM Access Analyzer.

Toma un template de CloudFormation ya sintetizado, extrae cada documento de
política IAM, sustituye las funciones intrínsecas por ARN de ejemplo y llama a
`aws accessanalyzer validate-policy`. No crea ni modifica recursos.

Uso:
    python3 tools/iac_policy_audit.py cdk.out/EnergySharkDev-ExistingInfrastructure.template.json
    python3 tools/iac_policy_audit.py template.json --profile arquisis --region us-east-2
"""

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

PLACEHOLDER_ACCOUNT = "111122223333"


def _arn_for_name(name: str, region: str) -> str:
    """Construye un ARN de ejemplo del servicio correcto según el nombre."""
    account = PLACEHOLDER_ACCOUNT
    lowered = name.lower()
    if "secret" in lowered:
        return f"arn:aws:secretsmanager:{region}:{account}:secret:placeholder-{name}"
    if "statemachine" in lowered or "workflow" in lowered:
        suffix = ":live" if "alias" in lowered else ""
        return (
            f"arn:aws:states:{region}:{account}:stateMachine:placeholder-{name}"
            f"{suffix}"
        )
    if "function" in lowered:
        return f"arn:aws:lambda:{region}:{account}:function:placeholder-{name}"
    if "loggroup" in lowered:
        return f"arn:aws:logs:{region}:{account}:log-group:/placeholder/{name}"
    if "table" in lowered:
        return f"arn:aws:dynamodb:{region}:{account}:table/{name}"
    if "bucket" in lowered:
        return f"arn:aws:s3:::placeholder-{lowered}"
    if "queue" in lowered:
        return f"arn:aws:sqs:{region}:{account}:{name}"
    if "topic" in lowered:
        return f"arn:aws:sns:{region}:{account}:{name}"
    return f"arn:aws:iam::{account}:role/placeholder-{name}"
PLACEHOLDER_PARTITION = "aws"
DEFAULT_REGION = "us-east-2"

ERROR_LEVELS = {"ERROR", "SECURITY_WARNING"}


@dataclass
class PolicyResult:
    origin: str
    findings: list[dict] = field(default_factory=list)

    @property
    def errors(self) -> list[dict]:
        return [
            finding
            for finding in self.findings
            if finding.get("findingType") in ERROR_LEVELS
        ]


def as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def resolve_placeholders(value, region: str = DEFAULT_REGION):
    """Reemplaza funciones intrínsecas por valores válidos para el validador."""
    if isinstance(value, list):
        return [resolve_placeholders(item, region) for item in value]
    if not isinstance(value, dict):
        return value

    if len(value) == 1:
        key, payload = next(iter(value.items()))
        if key == "Ref":
            if payload == "AWS::Partition":
                return PLACEHOLDER_PARTITION
            if payload == "AWS::AccountId":
                return PLACEHOLDER_ACCOUNT
            if payload == "AWS::Region":
                return region
            return _arn_for_name(str(payload), region)
        if key == "Fn::GetAtt":
            parts = as_list(payload)
            name = str(parts[0])
            attribute = str(parts[1]) if len(parts) > 1 else "Arn"
            if attribute == "Arn":
                return _arn_for_name(name, region)
            return f"{PLACEHOLDER_ACCOUNT}-{name}-{attribute}"
        if key in {"Fn::Select", "Fn::GetAZs"}:
            return f"{region}a"
        if key == "Fn::Sub":
            text = payload if isinstance(payload, str) else str(payload)
            return (
                text.replace("${AWS::Partition}", PLACEHOLDER_PARTITION)
                .replace("${AWS::AccountId}", PLACEHOLDER_ACCOUNT)
                .replace("${AWS::Region}", region)
                .replace("${AWS::URLSuffix}", "amazonaws.com")
            )
        if key == "Fn::Join":
            parts = payload[1] if isinstance(payload, list) and len(payload) > 1 else []
            return "".join(
                str(resolve_placeholders(item, region)) for item in as_list(parts)
            )
        if key in {"Fn::ImportValue", "Fn::Base64"}:
            return str(resolve_placeholders(payload, region))

    return {
        key: resolve_placeholders(item, region) for key, item in value.items()
    }


IDENTITY = "IDENTITY_POLICY"


def extract_policies(template: dict) -> list[tuple[str, dict, str]]:
    """Devuelve (origen, documento, tipo) por cada política IAM de permisos.

    Las políticas de confianza quedan fuera: solo dicen quién puede asumir el
    rol, no qué permisos concede el rol.
    """
    policies: list[tuple[str, dict, str]] = []
    for logical_id, resource in template.get("Resources", {}).items():
        resource_type = resource.get("Type")
        properties = resource.get("Properties", {})
        if resource_type == "AWS::IAM::Policy":
            document = properties.get("PolicyDocument")
            if isinstance(document, dict):
                policies.append((logical_id, document, IDENTITY))
        elif resource_type == "AWS::IAM::Role":
            for index, policy in enumerate(as_list(properties.get("Policies"))):
                document = policy.get("PolicyDocument")
                if isinstance(document, dict):
                    policies.append(
                        (f"{logical_id}/inline[{index}]", document, IDENTITY)
                    )
        elif resource_type == "AWS::IAM::ManagedPolicy":
            document = properties.get("PolicyDocument")
            if isinstance(document, dict):
                policies.append((logical_id, document, IDENTITY))
    return policies


def validate_policy(
    document: dict,
    *,
    region: str,
    profile: str | None,
    policy_type: str = IDENTITY,
) -> list[dict]:
    command = [
        "aws",
        "accessanalyzer",
        "validate-policy",
        "--policy-type",
        policy_type,
        "--policy-document",
        json.dumps(document),
        "--region",
        region,
        "--output",
        "json",
    ]
    if profile:
        command.extend(["--profile", profile])
    result = subprocess.run(
        command, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip().splitlines()
        raise RuntimeError(message[0] if message else "validate-policy falló")
    payload = json.loads(result.stdout or "{}")
    return payload.get("findings", [])


def audit(
    template_path: Path, *, region: str, profile: str | None
) -> list[PolicyResult]:
    template = json.loads(template_path.read_text(encoding="utf-8"))
    results: list[PolicyResult] = []
    for origin, document, policy_type in extract_policies(template):
        resolved = resolve_placeholders(document, region)
        findings = validate_policy(
            resolved,
            region=region,
            profile=profile,
            policy_type=policy_type,
        )
        results.append(PolicyResult(origin=origin, findings=findings))
    return results


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("template", help="Template de CloudFormation sintetizado")
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--profile", default=None)
    args = parser.parse_args(argv)

    path = Path(args.template)
    if not path.is_file():
        print(f"No existe el template: {path}", file=sys.stderr)
        return 2

    results = audit(path, region=args.region, profile=args.profile)
    if not results:
        print("El template no declara políticas IAM.")
        return 0

    errors = 0
    for result in results:
        errors += len(result.errors)
        status = "ERROR" if result.errors else "OK"
        print(f"{status} {result.origin} ({len(result.findings)} hallazgo(s))")
        for finding in result.findings:
            print(
                "  - {type}: {issue}".format(
                    type=finding.get("findingType"),
                    issue=finding.get("issueCode"),
                )
            )
    print(f"\nPolíticas: {len(results)} · errores: {errors}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
