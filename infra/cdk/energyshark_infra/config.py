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

    return DeploymentSettings(
        environment=environment,
        stack_prefix=stack_prefix,
        account=os.getenv("CDK_DEFAULT_ACCOUNT"),
        region=os.getenv("CDK_DEFAULT_REGION"),
    )
