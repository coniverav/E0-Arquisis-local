#!/usr/bin/env python3
"""E3-073: saneamiento de la política del bucket S3 del frontend.

Quita sentencias que conceden acceso a `Principal: "*"` y agrega una negación
de transporte inseguro. No aplica cambios: recibe un JSON y escribe otro.

Uso:
    python3 tools/prowler_s3_policy.py before.json after.json
"""

import argparse
import json
import sys

DENY_SID = "DenyInsecureTransport"


def _is_public(statement):
    principal = statement.get("Principal")
    if principal == "*":
        return True
    if isinstance(principal, dict):
        return "*" in principal.get("AWS", [])
    return False


def _resources(statement):
    resources = statement.get("Resource")
    if isinstance(resources, list):
        return resources
    return [resources] if resources else []


def bucket_arn(policy):
    for statement in policy.get("Statement", []):
        for resource in _resources(statement):
            if resource and resource.startswith("arn:aws:s3:::"):
                candidate = resource.split("/*")[0]
                if candidate and not candidate.endswith("*"):
                    return candidate
    raise ValueError("No se pudo inferir el ARN del bucket")


def sanitize_policy(policy):
    statements = [
        statement
        for statement in policy.get("Statement", [])
        if not _is_public(statement)
    ]
    arn = bucket_arn(policy)
    if not any(statement.get("Sid") == DENY_SID for statement in statements):
        statements.append(
            {
                "Sid": DENY_SID,
                "Effect": "Deny",
                "Principal": "*",
                "Action": "s3:*",
                "Resource": [arn, arn + "/*"],
                "Condition": {"Bool": {"aws:SecureTransport": "false"}},
            }
        )
    sanitized = dict(policy)
    sanitized["Statement"] = statements
    return sanitized


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before")
    parser.add_argument("after")
    args = parser.parse_args(argv)

    with open(args.before, encoding="utf-8") as handle:
        policy = json.load(handle)
    if isinstance(policy, str):
        policy = json.loads(policy)

    sanitized = sanitize_policy(policy)
    with open(args.after, "w", encoding="utf-8") as handle:
        json.dump(sanitized, handle, indent=2)
    print(json.dumps(sanitized, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
