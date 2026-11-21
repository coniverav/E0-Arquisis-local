from constructs import Construct

from aws_cdk import Stack


class MonitoringStack(Stack):
    """Límite futuro para métricas, alarmas y paneles de E3."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(
            scope,
            construct_id,
            description=(
                "EnergyShark: monitoreo y alertas de E3; "
                "sin recursos en la estructura base"
            ),
            **kwargs,
        )
