#!/usr/bin/env python3

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import aws_cdk as cdk

from energyshark_infra.config import load_settings
from energyshark_infra.stacks.existing_infrastructure import (
    ExistingInfrastructureStack,
)
from energyshark_infra.stacks.monitoring import MonitoringStack
from energyshark_infra.stacks.sagas import SagasStack


@dataclass(frozen=True)
class EnergySharkStacks:
    existing: ExistingInfrastructureStack | None
    sagas: SagasStack
    monitoring: MonitoringStack | None


def build_app(
    context: Mapping[str, Any] | None = None,
) -> tuple[cdk.App, EnergySharkStacks]:
    app = cdk.App(context=dict(context) if context is not None else None)
    settings = load_settings(app)
    aws_environment = cdk.Environment(
        account=settings.account,
        region=settings.region,
    )
    common = {
        "env": aws_environment,
        "termination_protection": settings.is_production,
    }
    deployment_scope = app.node.try_get_context("deploymentScope") or "all"
    if deployment_scope not in {"all", "sagas"}:
        raise ValueError("deploymentScope debe ser 'all' o 'sagas'")

    sagas = SagasStack(
        app,
        f"{settings.stack_prefix}-Sagas",
        settings=settings,
        **common,
    )
    existing = None
    monitoring = None
    if deployment_scope == "all":
        existing = ExistingInfrastructureStack(
            app,
            f"{settings.stack_prefix}-ExistingInfrastructure",
            settings=settings,
            **common,
        )
        monitoring = MonitoringStack(
            app,
            f"{settings.stack_prefix}-Monitoring",
            settings=settings,
            api=existing.api,
            instance=existing.instance,
            **common,
        )

    for stack in (item for item in (existing, sagas, monitoring) if item):
        cdk.Tags.of(stack).add("Project", "EnergyShark")
        cdk.Tags.of(stack).add("Environment", settings.environment)
        cdk.Tags.of(stack).add("ManagedBy", "AWS-CDK")

    return app, EnergySharkStacks(existing, sagas, monitoring)


if __name__ == "__main__":
    application, _ = build_app()
    application.synth()
