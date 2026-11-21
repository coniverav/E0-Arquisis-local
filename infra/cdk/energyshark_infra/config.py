import os
from dataclasses import dataclass

import aws_cdk as cdk


DEFAULT_STACK_PREFIXES = {
    "dev": "EnergySharkDev",
    "staging": "EnergySharkStaging",
    "prod": "EnergySharkProd",
}


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
    )
