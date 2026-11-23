"""Alertas y panel CloudWatch del stack E2 reconstruible.

Cubre la observabilidad nativa de AWS: errores y latencia del API Gateway,
estado de la instancia y CPU. Las cinco alertas de negocio de E2-061
(wallet, broker y market) viven en New Relic y se recrean con
`tools/newrelic_e2061.py`, no con CloudFormation.
"""

from aws_cdk import (
    CfnOutput,
    Duration,
    Stack,
    aws_apigatewayv2 as apigwv2,
    aws_cloudwatch as cloudwatch,
    aws_cloudwatch_actions as cloudwatch_actions,
    aws_ec2 as ec2,
    aws_sns as sns,
    aws_sns_subscriptions as sns_subscriptions,
)
from constructs import Construct

from energyshark_infra.config import DeploymentSettings


class MonitoringStack(Stack):
    """Alarmas y panel para el API y el host de aplicacion."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        settings: DeploymentSettings,
        api: apigwv2.HttpApi,
        instance: ec2.Instance,
        **kwargs,
    ) -> None:
        super().__init__(
            scope,
            construct_id,
            description=(
                "EnergyShark: alarmas y panel CloudWatch del API y el host"
            ),
            **kwargs,
        )
        self.settings = settings
        self._instance = instance

        self.alert_topic = sns.Topic(
            self,
            "AlertTopic",
            topic_name=f"{settings.stack_prefix.lower()}-alerts",
            display_name="EnergyShark alertas",
        )
        if settings.infrastructure.alarm_email:
            self.alert_topic.add_subscription(
                sns_subscriptions.EmailSubscription(
                    settings.infrastructure.alarm_email
                )
            )

        api_errors = self._alarm(
            "ApiServerErrors",
            "Errores 5xx del API por encima del umbral",
            api.metric_server_error(statistic="Sum", period=Duration.minutes(5)),
            threshold=1,
        )
        api_latency = self._alarm(
            "ApiLatencyP95",
            "Latencia p95 del API por encima de 2 segundos",
            api.metric_latency(statistic="p95", period=Duration.minutes(5)),
            threshold=2000,
            comparison_operator=(
                cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD
            ),
        )
        api_client_errors = self._alarm(
            "ApiClientErrors",
            "Volumen anormal de 4xx en el API",
            api.metric_client_error(statistic="Sum", period=Duration.minutes(5)),
            threshold=100,
        )
        instance_status = self._alarm(
            "InstanceStatusCheckFailed",
            "La instancia no responde a los chequeos de estado",
            self._instance_metric("StatusCheckFailed", period=Duration.minutes(5)),
            threshold=1,
        )
        instance_cpu = self._alarm(
            "InstanceCpuHigh",
            "CPU de la instancia por encima del 80 por ciento",
            self._instance_metric("CPUUtilization", period=Duration.minutes(5)),
            threshold=80,
            comparison_operator=(
                cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD
            ),
        )

        self.dashboard = cloudwatch.Dashboard(
            self,
            "OperationsDashboard",
            dashboard_name=f"{settings.stack_prefix}-operacion",
        )
        self.dashboard.add_widgets(
            cloudwatch.GraphWidget(
                title="API: peticiones y errores",
                left=[
                    api.metric_count(period=Duration.minutes(5)),
                    api.metric_server_error(
                        statistic="Sum", period=Duration.minutes(5)
                    ),
                    api.metric_client_error(
                        statistic="Sum", period=Duration.minutes(5)
                    ),
                ],
                width=12,
            ),
            cloudwatch.GraphWidget(
                title="API: latencia p50 y p95",
                left=[
                    api.metric_latency(statistic="p50", period=Duration.minutes(5)),
                    api.metric_latency(statistic="p95", period=Duration.minutes(5)),
                ],
                width=12,
            ),
            cloudwatch.GraphWidget(
                title="Host: CPU y estado",
                left=[
                    self._instance_metric("CPUUtilization", period=Duration.minutes(5)),
                    self._instance_metric(
                        "StatusCheckFailed", period=Duration.minutes(5)
                    ),
                ],
                width=12,
            ),
            cloudwatch.AlarmWidget(
                title="Alarmas del API",
                alarm=api_errors,
                width=6,
            ),
            cloudwatch.AlarmWidget(
                title="Alarmas del host",
                alarm=instance_status,
                width=6,
            ),
        )

        CfnOutput(
            self,
            "AlertTopicArn",
            value=self.alert_topic.topic_arn,
            description="Topic SNS de alertas del stack",
        )
        CfnOutput(
            self,
            "DashboardName",
            value=self.dashboard.dashboard_name,
        )

        for alarm in (
            api_errors,
            api_latency,
            api_client_errors,
            instance_status,
            instance_cpu,
        ):
            alarm.node.add_dependency(self.alert_topic)

    def _instance_metric(self, name: str, **kwargs) -> cloudwatch.Metric:
        return cloudwatch.Metric(
            namespace="AWS/EC2",
            metric_name=name,
            dimensions_map={"InstanceId": self._instance.instance_id},
            **kwargs,
        )

    def _alarm(
        self,
        construct_id: str,
        description: str,
        metric,
        *,
        threshold: float,
        comparison_operator: cloudwatch.ComparisonOperator | None = None,
    ) -> cloudwatch.Alarm:
        alarm = cloudwatch.Alarm(
            self,
            construct_id,
            metric=metric,
            threshold=threshold,
            evaluation_periods=1,
            comparison_operator=(
                comparison_operator
                or cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD
            ),
            alarm_description=description,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        alarm.add_alarm_action(
            cloudwatch_actions.SnsAction(self.alert_topic)
        )
        return alarm
