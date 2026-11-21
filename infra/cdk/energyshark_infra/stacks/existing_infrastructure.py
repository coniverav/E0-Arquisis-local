from constructs import Construct

from aws_cdk import Stack


class ExistingInfrastructureStack(Stack):
    """Límite para recursos de E2 que aún no administra CloudFormation."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(
            scope,
            construct_id,
            description=(
                "EnergyShark: límite de infraestructura E2 existente; "
                "sin recursos administrados en esta etapa"
            ),
            **kwargs,
        )
