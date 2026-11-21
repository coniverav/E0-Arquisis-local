from constructs import Construct

from aws_cdk import Stack


class SagasStack(Stack):
    """Límite futuro para Step Functions y Lambdas de las sagas."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(
            scope,
            construct_id,
            description=(
                "EnergyShark: Step Functions y Lambda de sagas; "
                "sin recursos en la estructura base"
            ),
            **kwargs,
        )
