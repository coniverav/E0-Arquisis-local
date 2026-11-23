#!/usr/bin/env python3
"""E3-071/E3-075: triage y comparación de reportes Prowler.

Lee el reporte baseline y el posterior (JSON OCSF o CSV de Prowler 5.x) y
produce tablas Markdown sin modificar ningún recurso AWS.

Uso:
    python3 tools/prowler_report.py triage baseline/
    python3 tools/prowler_report.py compare baseline/ after/
    python3 tools/prowler_report.py compare baseline.ocsf.json after.ocsf.json
"""

import argparse
import csv
import json
import sys
from pathlib import Path

SEVERITY_ORDER = {
    "informational": 0,
    "info": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
    "fatal": 5,
}

SEVERITY_BY_ID = {
    0: "informational",
    1: "informational",
    2: "low",
    3: "medium",
    4: "high",
    5: "critical",
    6: "fatal",
}

FAIL_STATUSES = {"fail", "failed", "new", "warning"}

# En OCSF de Prowler, `status` describe el ciclo de vida del hallazgo ("New")
# y el resultado real del check viene en `status_code` ("PASS"/"FAIL").
RESULT_STATUSES = {"pass", "fail", "failed", "warning", "manual"}

# Coincidencias con los candidatos registrados en docs/E3-011-prowler-baseline.md
CANDIDATES = (
    ("ec2_ebs_default_encryption", "Cifrado EBS predeterminado"),
    ("s3_bucket_level_public_access_block", "Bloqueo de acceso público S3"),
    ("s3_bucket_public_access", "Bucket S3 con acceso público"),
    ("iam_user_mfa_enabled_console", "MFA de usuario IAM con consola"),
)


def _first(mapping, *keys):
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return None


def _normalize_severity(value):
    if value is None:
        return "informational"
    if isinstance(value, int):
        return SEVERITY_BY_ID.get(value, "informational")
    text = str(value).strip().lower()
    if text.isdigit():
        return SEVERITY_BY_ID.get(int(text), "informational")
    return text if text in SEVERITY_ORDER else "informational"


def _is_failed(status):
    if status is None:
        return False
    text = str(status).strip().lower()
    return text in FAIL_STATUSES


def _result_status(raw):
    """Devuelve el resultado del check, no el ciclo de vida del hallazgo."""
    for key in ("status_code", "StatusCode", "STATUS", "Status", "status"):
        value = raw.get(key)
        if value not in (None, ""):
            if str(value).strip().lower() in RESULT_STATUSES:
                return value
    return _first(
        raw,
        "status_code",
        "StatusCode",
        "STATUS",
        "Status",
        "status",
        "status_extended",
    )


def _normalize_finding(raw, source):
    finding_info = raw.get("finding_info") or {}
    analytic = finding_info.get("analytic") or {}
    resource = None
    resources = raw.get("resources")
    if isinstance(resources, list) and resources:
        resource = _first(resources[0], "uid", "name", "arn")
    resource = resource or _first(
        raw,
        "ResourceUid",
        "RESOURCE_UID",
        "resource_uid",
        "ResourceArn",
        "RESOURCE_ARN",
        "resource_arn",
        "ResourceName",
        "RESOURCE_NAME",
        "resource_name",
        "resource",
    )

    severity = _normalize_severity(
        _first(
            raw,
            "severity",
            "SEVERITY",
            "Severity",
            "severity_id",
        )
    )
    status = _result_status(raw)
    compliance = raw.get("compliance")
    if isinstance(compliance, dict) and compliance.get("status") is not None:
        status = compliance["status"]

    return {
        "check_id": str(
            _first(
                raw,
                "CheckID",
                "CHECK_ID",
                "check_id",
                "check",
                "uid",
            )
            or analytic.get("uid")
            or finding_info.get("uid")
            or "unknown"
        ),
        "title": _first(
            raw,
            "CheckTitle",
            "CHECK_TITLE",
            "check_title",
            "title",
            "message",
        )
        or analytic.get("name")
        or finding_info.get("title"),
        "severity": severity,
        "status": status,
        "resource": resource,
        "source": source,
    }


def load_findings(path):
    """Carga hallazgos de un archivo o del primer reporte válido de una carpeta."""
    path = Path(path)
    files = [path]
    if path.is_dir():
        files = sorted(
            [
                candidate
                for candidate in path.rglob("*")
                if candidate.suffix.lower() in {".json", ".csv"}
            ]
        )
    for file_path in files:
        findings = _load_file(file_path)
        if findings:
            return findings
    raise ValueError(f"Sin hallazgos legibles en {path}")


def _load_file(file_path):
    text = file_path.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        return []
    if file_path.suffix.lower() == ".csv" or _looks_like_csv(text):
        return _load_csv(text, file_path.name)
    return _load_json(text, file_path.name)


def _looks_like_csv(text):
    first = text.lstrip().splitlines()[0] if text.strip() else ""
    return ";" in first and ("CHECK_ID" in first.upper() or "SEVERITY" in first.upper())


def _load_json(text, source):
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return []
    if isinstance(payload, dict):
        for key in ("findings", "Findings", "results", "data"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
        else:
            payload = [payload]
    if not isinstance(payload, list):
        return []
    return [
        _normalize_finding(item, source)
        for item in payload
        if isinstance(item, dict)
    ]


def _load_csv(text, source):
    sample = text[:2048]
    delimiter = ";" if sample.count(";") >= sample.count(",") else ","
    rows = list(csv.DictReader(text.splitlines(), delimiter=delimiter))
    return [
        _normalize_finding({key.strip(): value for key, value in row.items() if key}, source)
        for row in rows
        if row
    ]


def _severity_rank(finding):
    return SEVERITY_ORDER.get(finding["severity"], 0)


def _failed(findings):
    return [finding for finding in findings if _is_failed(finding["status"])]


def _match_candidates(findings):
    matches = {}
    lower = [(check_id, label) for check_id, label in CANDIDATES]
    for check_id, label in lower:
        for finding in findings:
            if finding["check_id"].lower() == check_id:
                matches[check_id] = (label, finding)
                break
    for check_id, label in lower:
        if check_id in matches:
            continue
        for finding in findings:
            if check_id in finding["check_id"].lower():
                matches[check_id] = (label, finding)
                break
    return matches


def _escape(value):
    return str(value or "—").replace("|", "\\|")


def render_triage(findings, limit=20):
    failed = sorted(
        _failed(findings),
        key=lambda finding: (_severity_rank(finding), finding["check_id"]),
        reverse=True,
    )
    medium_plus = [
        finding for finding in failed if _severity_rank(finding) >= SEVERITY_ORDER["medium"]
    ]
    lines = [
        f"Hallazgos totales: {len(findings)} · fallidos: {len(failed)} · "
        f"MEDIUM+: {len(medium_plus)}",
        "",
        "| # | Check ID | Severidad | Recurso | Estado |",
        "| ---: | :--- | :--- | :--- | :--- |",
    ]
    for index, finding in enumerate(failed[:limit], start=1):
        lines.append(
            "| {index} | `{check}` | {severity} | {resource} | {status} |".format(
                index=index,
                check=_escape(finding["check_id"]),
                severity=_escape(finding["severity"]),
                resource=_escape(finding["resource"]),
                status=_escape(finding["status"]),
            )
        )

    matched = _match_candidates(failed)
    if matched:
        lines.extend(["", "## Candidatos de docs/E3-011", ""])
        lines.append("| Check | Candidato | Severidad | Recurso | Estado |")
        lines.append("| :--- | :--- | :--- | :--- | :--- |")
        for check_id, (label, finding) in matched.items():
            lines.append(
                "| `{check}` | {label} | {severity} | {resource} | {status} |".format(
                    check=_escape(check_id),
                    label=_escape(label),
                    severity=_escape(finding["severity"]),
                    resource=_escape(finding["resource"]),
                    status=_escape(finding["status"]),
                )
            )
    return "\n".join(lines)


def _by_check(findings):
    grouped = {}
    for finding in findings:
        grouped.setdefault(finding["check_id"], []).append(finding)
    return grouped


def render_compare(before, after):
    groups_before = _by_check(before)
    groups_after = _by_check(after)
    check_ids = sorted(set(groups_before) | set(groups_after))
    lines = []
    fixed = regressed = 0
    rows = []
    for check_id in check_ids:
        old = groups_before.get(check_id, [])
        new = groups_after.get(check_id, [])
        old_failed = len(_failed(old))
        new_failed = len(_failed(new))
        severity = (new or old)[0]["severity"] if (new or old) else "informational"
        if old_failed > 0 and new_failed == 0:
            fixed += 1
            verdict = "corregido"
        elif old_failed == 0 and new_failed > 0:
            regressed += 1
            verdict = "regresión"
        elif old_failed > new_failed:
            verdict = "mejora parcial"
        elif new_failed > old_failed:
            verdict = "empeoró"
        else:
            verdict = "sin cambio"
        if verdict != "sin cambio":
            rows.append((severity, check_id, old_failed, new_failed, verdict))

    rows.sort(key=lambda row: (SEVERITY_ORDER.get(row[0], 0), row[1]), reverse=True)
    lines.append(
        f"Checks comparados: {len(check_ids)} · corregidos: {fixed} · "
        f"regresiones: {regressed}"
    )
    lines.extend(
        [
            "",
            "| Check ID | Severidad | Fallidos antes | Fallidos después | Resultado |",
            "| :--- | :--- | ---: | ---: | :--- |",
        ]
    )
    for severity, check_id, old_failed, new_failed, verdict in rows:
        lines.append(
            f"| `{_escape(check_id)}` | {severity} | {old_failed} | {new_failed} | {verdict} |"
        )
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    triage = subparsers.add_parser("triage", help="listar hallazgos MEDIUM+ fallidos")
    triage.add_argument("report", help="archivo o carpeta del reporte Prowler")
    triage.add_argument("--limit", type=int, default=20)

    compare = subparsers.add_parser("compare", help="comparar antes y después")
    compare.add_argument("before", help="reporte baseline")
    compare.add_argument("after", help="reporte posterior")

    args = parser.parse_args(argv)
    if args.command == "triage":
        print(render_triage(load_findings(args.report), limit=args.limit))
    else:
        print(render_compare(load_findings(args.before), load_findings(args.after)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
