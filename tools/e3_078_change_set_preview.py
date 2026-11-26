#!/usr/bin/env python3
"""E3-078: previsualiza el plan de despliegue sin aplicar cambios.

Parte de un template ya sintetizado con CDK, crea un *change set* de
CloudFormation, describe los recursos que se crearian o modificarian y lo
elimina. Nunca ejecuta el change set: el despliegue real queda fuera de esta
herramienta a proposito.

Garantias:
    * No llama a ExecuteChangeSet. El unico camino de escritura es crear y
      borrar el change set.
    * Con `--change-set-type CREATE` (por defecto) rechaza stacks existentes que
      no esten en REVIEW_IN_PROGRESS, para no tocar recursos productivos.
    * Limpia el change set siempre. Si el stack quedo en REVIEW_IN_PROGRESS por
      esta corrida, tambien lo elimina.
    * No crea buckets. Si el template supera el limite de 51200 bytes de
      `--template-body`, exige `--upload-bucket` con un bucket existente (el
      objeto se borra al terminar) o `--template-url`.

Uso:
    python3 tools/e3_078_change_set_preview.py cdk.out/ExistingInfrastructure.template.json \
        --stack-name e3078-preview-existing
    python3 tools/e3_078_change_set_preview.py cdk.out/Monitoring.template.json \
        --stack-name e3078-preview-monitoring --output markdown
"""

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

TEMPLATE_BODY_LIMIT = 51_200
BOOTSTRAP_PARAMETER = "BootstrapVersion"
TERMINAL_FAILURE_STATUSES = frozenset(
    {"FAILED", "DELETE_COMPLETE", "DELETE_FAILED", "ROLLBACK_COMPLETE"}
)


@dataclass
class PreviewResult:
    stack_name: str
    change_set_name: str
    change_set_type: str
    status: str
    stack_created: bool
    changes: list = field(default_factory=list)
    reasons: list = field(default_factory=list)
    error: str | None = None

    def summary(self) -> dict:
        counts: dict[str, int] = {}
        for change in self.changes:
            # La API ubica Action dentro de ResourceChange; se acepta el nivel
            # superior por compatibilidad con respuestas simplificadas.
            resource = change.get("ResourceChange") or {}
            action = resource.get("Action") or change.get("Action") or "Unknown"
            counts[action] = counts.get(action, 0) + 1
        return {
            "stack_name": self.stack_name,
            "change_set_name": self.change_set_name,
            "change_set_type": self.change_set_type,
            "status": self.status,
            "stack_created": self.stack_created,
            "total_resources": len(self.changes),
            "by_action": counts,
            "changes": self.changes,
            "reasons": self.reasons,
            "error": self.error,
        }


def strip_bootstrap_rule(template: dict) -> dict:
    """Quita el guardia BootstrapVersion de un template CDK.

    CDK agrega una Rule que compara el parametro `BootstrapVersion` con el
    valor publicado en SSM. CloudFormation rechaza el change set si la cuenta o
    la region no fueron bootstrapeadas. El guardia no describe recursos, asi que
    se elimina solo de la vista previa; el template original no se modifica.
    """
    cleaned = json.loads(json.dumps(template))

    rules = cleaned.get("Rules") or {}
    for name in list(rules):
        assertions = rules[name].get("Assertions") or []
        if any(
            isinstance(assertion, dict)
            and "BootstrapVersion" in json.dumps(assertion.get("Assert", {}))
            for assertion in assertions
        ):
            rules.pop(name, None)
    if not rules:
        cleaned.pop("Rules", None)

    parameters = cleaned.get("Parameters") or {}
    parameters.pop(BOOTSTRAP_PARAMETER, None)
    if not parameters:
        cleaned.pop("Parameters", None)

    return cleaned


def load_template_body(path: str | Path) -> str:
    template = json.loads(Path(path).read_text(encoding="utf-8"))
    return json.dumps(strip_bootstrap_rule(template), separators=(",", ":"))


def template_object_key(stack_name: str, *, timestamp: int | None = None) -> str:
    stamp = int(time.time()) if timestamp is None else timestamp
    return f"e3-078/{stack_name}-{stamp}.json"


def upload_template(s3_client, bucket: str, key: str, body: str) -> str:
    """Sube el template a un bucket existente y devuelve la URL para CloudFormation.

    Solo se usa cuando el template supera el limite de `--template-body`. No
    crea buckets: exige uno existente y borra el objeto al terminar.
    """
    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=body.encode("utf-8"),
        ContentType="application/json",
        ServerSideEncryption="AES256",
    )
    return f"https://{bucket}.s3.amazonaws.com/{key}"


def delete_template(s3_client, bucket: str, key: str) -> None:
    s3_client.delete_object(Bucket=bucket, Key=key)


def stack_status(client, stack_name: str) -> str | None:
    """Devuelve el estado del stack o None si no existe."""
    try:
        response = client.describe_stacks(StackName=stack_name)
    except Exception as exc:  # botocore ClientError con ValidationError
        message = str(exc)
        if "does not exist" in message or "ValidationError" in message:
            return None
        raise
    stacks = response.get("Stacks") or []
    if not stacks:
        return None
    return stacks[0].get("StackStatus")


def wait_for_change_set(client, stack_name: str, change_set_name: str,
                        *, timeout_s: float, poll_s: float) -> dict:
    deadline = time.monotonic() + timeout_s
    while True:
        response = client.describe_change_set(
            StackName=stack_name, ChangeSetName=change_set_name
        )
        status = response.get("Status")
        if status in {"CREATE_COMPLETE", "FAILED"}:
            return response
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"El change set {change_set_name} sigue en {status} tras {timeout_s}s"
            )
        time.sleep(poll_s)


def preview(
    client,
    *,
    stack_name: str,
    change_set_name: str,
    change_set_type: str = "CREATE",
    template_body: str | None = None,
    template_url: str | None = None,
    capabilities: tuple[str, ...] = ("CAPABILITY_NAMED_IAM",),
    parameters: tuple[dict[str, str], ...] = (),
    timeout_s: float = 300.0,
    poll_s: float = 3.0,
) -> PreviewResult:
    """Crea, describe y elimina un change set. Nunca lo ejecuta."""
    if change_set_type not in {"CREATE", "UPDATE"}:
        raise ValueError("change_set_type debe ser CREATE o UPDATE")
    if bool(template_body) == bool(template_url):
        raise ValueError("Indica template_body o template_url, no ambos")

    existing = stack_status(client, stack_name)
    if change_set_type == "CREATE" and existing not in (None, "REVIEW_IN_PROGRESS"):
        raise SystemExit(
            f"El stack {stack_name} ya existe en estado {existing}. "
            "Usa --change-set-type UPDATE para previsualizar sin crear un stack nuevo."
        )
    if change_set_type == "UPDATE" and existing is None:
        raise SystemExit(
            f"El stack {stack_name} no existe; usa --change-set-type CREATE."
        )

    result = PreviewResult(
        stack_name=stack_name,
        change_set_name=change_set_name,
        change_set_type=change_set_type,
        status="NOT_STARTED",
        stack_created=existing is None,
    )

    request = {
        "StackName": stack_name,
        "ChangeSetName": change_set_name,
        "ChangeSetType": change_set_type,
        "Capabilities": list(capabilities),
    }
    if template_body:
        request["TemplateBody"] = template_body
    else:
        request["TemplateURL"] = template_url
    if parameters:
        request["Parameters"] = list(parameters)

    try:
        client.create_change_set(**request)
        described = wait_for_change_set(
            client, stack_name, change_set_name,
            timeout_s=timeout_s, poll_s=poll_s,
        )
        result.status = described.get("Status", "UNKNOWN")
        result.changes = described.get("Changes") or []
        result.reasons = described.get("StatusReason") and [
            described["StatusReason"]
        ] or []
        if result.status != "CREATE_COMPLETE":
            result.error = described.get("StatusReason", "change set no ejecutable")
    finally:
        # Orden inverso: primero el change set, despues el stack si lo creamos.
        try:
            client.delete_change_set(StackName=stack_name, ChangeSetName=change_set_name)
        except Exception:
            pass
        if result.stack_created:
            try:
                client.delete_stack(StackName=stack_name)
            except Exception:
                pass

    return result


def render(result: PreviewResult, output: str) -> str:
    summary = result.summary()
    if output == "json":
        return json.dumps(summary, indent=2, ensure_ascii=False, default=str)

    lines = [
        f"# E3-078 vista previa · {summary['stack_name']}",
        "",
        f"- **Change set:** {summary['change_set_name']} ({summary['change_set_type']})",
        f"- **Estado:** {summary['status']}",
        f"- **Recursos:** {summary['total_resources']}",
        f"- **Por accion:** {summary['by_action'] or 'sin cambios'}",
        f"- **Stack creado y eliminado por esta corrida:** "
        f"{'si' if summary['stack_created'] else 'no (preexistente)'}",
    ]
    if summary["error"]:
        lines.append(f"- **Error:** {summary['error']}")
    if output == "markdown":
        lines += ["", "| Accion | Recurso | Tipo |", "| --- | --- | --- |"]
        for change in summary["changes"]:
            resource = change.get("ResourceChange") or {}
            lines.append(
                "| {action} | {logical} | {kind} |".format(
                    action=resource.get("Action", "?"),
                    logical=resource.get("LogicalResourceId", "?"),
                    kind=resource.get("ResourceType", "?"),
                )
            )
    else:
        for change in summary["changes"]:
            resource = change.get("ResourceChange") or {}
            lines.append(
                "{action:<8} {logical:<40} {kind}".format(
                    action=resource.get("Action", "?"),
                    logical=resource.get("LogicalResourceId", "?"),
                    kind=resource.get("ResourceType", "?"),
                )
            )
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="E3-078: describe un change set y lo elimina, sin aplicarlo"
    )
    parser.add_argument("template", nargs="?", help="Template CloudFormation sintetizado")
    parser.add_argument("--stack-name", required=True)
    parser.add_argument("--change-set-name", default=None)
    parser.add_argument(
        "--change-set-type", default="CREATE", choices=["CREATE", "UPDATE"]
    )
    parser.add_argument("--template-url", default=None)
    parser.add_argument(
        "--upload-bucket",
        default=None,
        help="Bucket existente para templates que superan 51200 bytes; el objeto se "
        "borra al terminar",
    )
    parser.add_argument("--region", default=None)
    parser.add_argument("--profile", default=None)
    parser.add_argument(
        "--capabilities",
        nargs="*",
        default=["CAPABILITY_NAMED_IAM"],
        help="Capacidades IAM del template (por defecto CAPABILITY_NAMED_IAM)",
    )
    parser.add_argument(
        "--parameter", action="append", default=[], metavar="CLAVE=VALOR"
    )
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--poll", type=float, default=3.0)
    parser.add_argument(
        "--output", default="json", choices=["json", "markdown", "table"]
    )
    args = parser.parse_args(argv)

    if args.template and args.template_url:
        parser.error("Usa el template posicional o --template-url, no ambos")
    if not args.template and not args.template_url:
        parser.error("Falta el template o --template-url")

    parameters = []
    for item in args.parameter:
        if "=" not in item:
            parser.error(f"Parametro invalido: {item}")
        key, value = item.split("=", 1)
        parameters.append({"ParameterKey": key, "ParameterValue": value})

    template_body = None
    if args.template:
        template_body = load_template_body(args.template)
        if len(template_body.encode("utf-8")) > TEMPLATE_BODY_LIMIT:
            if not args.upload_bucket:
                parser.error(
                    "El template supera 51200 bytes. Indica --upload-bucket con un "
                    "bucket existente (el objeto se borra al terminar) o usa "
                    "--template-url. Esta herramienta no crea buckets."
                )

    import boto3  # import local: los tests inyectan un cliente falso

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    client = session.client("cloudformation")
    change_set_name = args.change_set_name or f"{args.stack_name}-preview"

    template_url = args.template_url
    s3_client = None
    uploaded_key = None
    if template_body and len(template_body.encode("utf-8")) > TEMPLATE_BODY_LIMIT:
        s3_client = session.client("s3")
        uploaded_key = template_object_key(args.stack_name)
        template_url = upload_template(
            s3_client, args.upload_bucket, uploaded_key, template_body
        )
        template_body = None

    try:
        result = preview(
            client,
            stack_name=args.stack_name,
            change_set_name=change_set_name,
            change_set_type=args.change_set_type,
            template_body=template_body,
            template_url=template_url,
            capabilities=tuple(args.capabilities),
            parameters=tuple(parameters),
            timeout_s=args.timeout,
            poll_s=args.poll,
        )
    finally:
        if s3_client is not None and uploaded_key is not None:
            try:
                delete_template(s3_client, args.upload_bucket, uploaded_key)
            except Exception:
                print(
                    f"Advertencia: no se pudo borrar s3://{args.upload_bucket}/{uploaded_key}",
                    file=sys.stderr,
                )

    print(render(result, args.output))
    return 0 if result.status == "CREATE_COMPLETE" else 1


if __name__ == "__main__":
    sys.exit(main())
