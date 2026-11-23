import os
from dataclasses import dataclass

import aws_cdk as cdk
from aws_cdk import aws_logs as logs


LOG_RETENTION_OPTIONS = {
    1: logs.RetentionDays.ONE_DAY,
    3: logs.RetentionDays.THREE_DAYS,
    5: logs.RetentionDays.FIVE_DAYS,
    7: logs.RetentionDays.ONE_WEEK,
    14: logs.RetentionDays.TWO_WEEKS,
    30: logs.RetentionDays.ONE_MONTH,
    60: logs.RetentionDays.TWO_MONTHS,
    90: logs.RetentionDays.THREE_MONTHS,
}


def log_retention_for(days: int) -> logs.RetentionDays:
    """Traduce dias a una retencion valida de CloudWatch Logs."""
    try:
        return LOG_RETENTION_OPTIONS[days]
    except KeyError as exc:
        raise ValueError(
            f"Retencion de logs no soportada: {days}. "
            f"Use uno de {sorted(LOG_RETENTION_OPTIONS)}"
        ) from exc


DEFAULT_STACK_PREFIXES = {
    "dev": "EnergySharkDev",
    "staging": "EnergySharkStaging",
    "prod": "EnergySharkProd",
}

# Valores por defecto del stack E2. La cuenta productiva usa la VPC por
# defecto, una sola instancia t3.micro y el bucket del frontend servido por
# CloudFront; aqui se declara una red propia para no depender de la VPC
# por defecto de la cuenta donde se reconstruya.
DEFAULT_NETWORK_CIDR = "10.20.0.0/16"
DEFAULT_INSTANCE_TYPE = "t3.micro"
DEFAULT_AMI_ID = "ami-0e5497a77ef21b5ac"
DEFAULT_KEY_NAME = "arquisisg10"
# Vacio por defecto: el acceso administrativo va por Session Manager.
DEFAULT_ADMIN_CIDR = ""
DEFAULT_API_BACKEND_URL = "https://energyshark-g10.tech"
DEFAULT_JWT_ISSUER = "https://dev-rz37e3yw6i3fn2bn.us.auth0.com/"
DEFAULT_JWT_AUDIENCE = "https://arquisis-e1-api/"


@dataclass(frozen=True)
class InfrastructureSettings:
    """Parametros del stack E2 reconstruible."""

    network_cidr: str
    instance_type: str
    ami_ids: dict[str, str]
    key_name: str
    admin_cidr: str | None
    frontend_bucket_name: str
    frontend_domain_name: str | None
    certificate_arn: str | None
    api_backend_url: str
    jwt_issuer: str
    jwt_audience: str
    cors_origins: tuple[str, ...]
    app_repository_url: str
    alarm_email: str | None


@dataclass(frozen=True)
class DeploymentSettings:
    environment: str
    stack_prefix: str
    account: str | None
    region: str | None
    seller_saga_timeout_seconds: int
    buyer_saga_timeout_seconds: int
    lambda_timeout_seconds: int
    lambda_memory_mb: int
    log_retention_days: int
    infrastructure: InfrastructureSettings

    @property
    def is_production(self) -> bool:
        return self.environment == "prod"


def load_settings(app: cdk.App) -> DeploymentSettings:
    environment = (
        app.node.try_get_context("environment")
        or os.getenv("ENERGYSHARK_ENV")
        or "dev"
    )
    if environment not in DEFAULT_STACK_PREFIXES:
        supported = ", ".join(DEFAULT_STACK_PREFIXES)
        raise ValueError(f"Ambiente no soportado: {environment}. Use: {supported}")

    environments = app.node.try_get_context("environments") or {}
    environment_config = environments.get(environment, {})
    stack_prefix = environment_config.get(
        "stackPrefix",
        DEFAULT_STACK_PREFIXES[environment],
    )

    def positive_int(name: str, default: int) -> int:
        value = int(environment_config.get(name, default))
        if value <= 0:
            raise ValueError(f"{name} debe ser mayor que cero")
        return value

    def text(name: str, default: str | None) -> str | None:
        value = environment_config.get(name, default)
        if value is None:
            return None
        value = str(value).strip()
        return value or None

    def required_text(name: str, default: str) -> str:
        value = text(name, default)
        if value is None:
            raise ValueError(f"{name} no puede estar vacio")
        return value

    ami_ids = environment_config.get("amiIds") or {"us-east-2": DEFAULT_AMI_ID}
    cors_origins = tuple(
        environment_config.get("corsOrigins")
        or (
            f"https://{text('frontendDomainName', None) or 'app.energyshark-g10.tech'}",
        )
    )

    return DeploymentSettings(
        environment=environment,
        stack_prefix=stack_prefix,
        account=os.getenv("CDK_DEFAULT_ACCOUNT"),
        region=os.getenv("CDK_DEFAULT_REGION"),
        seller_saga_timeout_seconds=positive_int(
            "sellerSagaTimeoutSeconds",
            1800,
        ),
        buyer_saga_timeout_seconds=positive_int(
            "buyerSagaTimeoutSeconds",
            1800,
        ),
        lambda_timeout_seconds=positive_int("lambdaTimeoutSeconds", 25),
        lambda_memory_mb=positive_int("lambdaMemoryMb", 256),
        log_retention_days=positive_int("logRetentionDays", 30),
        infrastructure=InfrastructureSettings(
            network_cidr=required_text("networkCidr", DEFAULT_NETWORK_CIDR),
            instance_type=required_text("instanceType", DEFAULT_INSTANCE_TYPE),
            ami_ids={str(key): str(value) for key, value in ami_ids.items()},
            key_name=required_text("keyName", DEFAULT_KEY_NAME),
            admin_cidr=text("adminCidr", DEFAULT_ADMIN_CIDR),
            frontend_bucket_name=required_text(
                "frontendBucketName",
                f"energyshark-{environment}-frontend",
            ),
            frontend_domain_name=text("frontendDomainName", None),
            certificate_arn=text("certificateArn", None),
            api_backend_url=required_text("apiBackendUrl", DEFAULT_API_BACKEND_URL).rstrip("/"),
            jwt_issuer=required_text("jwtIssuer", DEFAULT_JWT_ISSUER),
            jwt_audience=required_text("jwtAudience", DEFAULT_JWT_AUDIENCE),
            cors_origins=cors_origins,
            app_repository_url=text("appRepositoryUrl", "") or "",
            alarm_email=text("alarmEmail", None),
        ),
    )
