import json
import re
from dataclasses import dataclass
from pathlib import Path

from aws_cdk import (
    CfnOutput,
    CfnParameter,
    Duration,
    RemovalPolicy,
    Stack,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_stepfunctions as sfn,
)
from constructs import Construct

from energyshark_infra.config import DeploymentSettings


@dataclass(frozen=True)
class SagaTask:
    task_name: str
    function_placeholder: str
    secret_kinds: tuple[str, ...] = ()


SELLER_TASKS = (
    SagaTask("reserve-seller", "ReserveSellerFunctionArn", ("database",)),
    SagaTask(
        "reject-seller-bid",
        "RejectSellerBidFunctionArn",
        ("database", "broker"),
    ),
    SagaTask(
        "seller-proposal",
        "SellerProposalFunctionArn",
        ("database", "wallet"),
    ),
    SagaTask(
        "reconcile-seller-swap",
        "ReconcileSellerSwapFunctionArn",
        ("database", "wallet"),
    ),
    SagaTask(
        "publish-bid-accepted",
        "PublishBidAcceptedFunctionArn",
        ("database", "broker"),
    ),
    SagaTask("register-callback", "RegisterCallbackFunctionArn", ("database",)),
    SagaTask(
        "cancel-seller-swap",
        "CancelSellerSwapFunctionArn",
        ("database", "wallet"),
    ),
    SagaTask(
        "compensate-seller",
        "CompensateSellerFunctionArn",
        ("database", "broker"),
    ),
)

BUYER_TASKS = (
    SagaTask(
        "validate-buyer-token",
        "ValidateBuyerTokenFunctionArn",
        ("database",),
    ),
    SagaTask("check-buyer-deadline", "CheckBuyerDeadlineFunctionArn"),
    SagaTask(
        "buyer-acceptance",
        "BuyerAcceptanceFunctionArn",
        ("database", "wallet"),
    ),
    SagaTask(
        "reconcile-buyer-swap",
        "ReconcileBuyerSwapFunctionArn",
        ("database", "wallet"),
    ),
)

SHARED_TASKS = (
    SagaTask("finalize-saga", "FinalizeSagaFunctionArn", ("database",)),
)

LOG_DELIVERY_ACTIONS = (
    "logs:CreateLogDelivery",
    "logs:GetLogDelivery",
    "logs:UpdateLogDelivery",
    "logs:DeleteLogDelivery",
    "logs:ListLogDeliveries",
    "logs:PutResourcePolicy",
    "logs:DescribeResourcePolicies",
    "logs:DescribeLogGroups",
)


class SagasStack(Stack):
    """Step Functions, Lambdas y permisos mínimos para las sagas E3."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        settings: DeploymentSettings,
        **kwargs,
    ) -> None:
        super().__init__(
            scope,
            construct_id,
            description="EnergyShark: sagas Standard, Lambdas e IAM mínimo",
            **kwargs,
        )
        self.settings = settings
        self._retention = self._log_retention(settings.log_retention_days)
        self._secret_parameters = self._create_secret_parameters()
        self.task_functions = self._create_task_functions()

        self.seller_state_machine, self.seller_alias = self._create_state_machine(
            role="seller",
            definition_file="seller.asl.json",
            tasks=SELLER_TASKS + SHARED_TASKS,
            timeout_seconds=settings.seller_saga_timeout_seconds,
        )
        self.buyer_state_machine, self.buyer_alias = self._create_state_machine(
            role="buyer",
            definition_file="buyer.asl.json",
            tasks=BUYER_TASKS + SHARED_TASKS,
            timeout_seconds=settings.buyer_saga_timeout_seconds,
        )
        self.connector_saga_role = self._create_connector_role()
        self.connector_instance_profile = iam.CfnInstanceProfile(
            self,
            "ConnectorSagaInstanceProfile",
            instance_profile_name=(
                f"energyshark-{settings.environment}-connector-sagas"
            ),
            roles=[self.connector_saga_role.role_name],
        )
        self.connector_instance_profile.node.add_dependency(
            self.connector_saga_role
        )

        CfnOutput(
            self,
            "SellerSagaAliasArn",
            value=self.seller_alias.ref,
            description="ARN versionado para iniciar la saga seller",
        )
        CfnOutput(
            self,
            "BuyerSagaAliasArn",
            value=self.buyer_alias.ref,
            description="ARN versionado para iniciar la saga buyer",
        )
        CfnOutput(
            self,
            "ConnectorSagaRoleArn",
            value=self.connector_saga_role.role_arn,
            description="Rol mínimo para StartExecution y callbacks desde EC2",
        )
        CfnOutput(
            self,
            "ConnectorSagaInstanceProfileName",
            value=self.connector_instance_profile.ref,
            description="Instance profile para asociar al host de Connector",
        )

    def _create_secret_parameters(self) -> dict[str, CfnParameter]:
        return {
            "database": CfnParameter(
                self,
                "DatabaseSecretArn",
                type="String",
                no_echo=True,
                description="ARN de Secrets Manager con acceso a PostgreSQL",
            ),
            "wallet": CfnParameter(
                self,
                "WalletSecretArn",
                type="String",
                no_echo=True,
                description="ARN de Secrets Manager con configuración Wallet",
            ),
            "broker": CfnParameter(
                self,
                "BrokerSecretArn",
                type="String",
                no_echo=True,
                description="ARN de Secrets Manager con configuración RabbitMQ",
            ),
        }

    def _create_task_functions(self) -> dict[str, lambda_.Function]:
        source = Path(__file__).resolve().parents[2] / "lambda_src" / "saga_task"
        functions: dict[str, lambda_.Function] = {}
        seen: set[str] = set()

        for task in SELLER_TASKS + BUYER_TASKS + SHARED_TASKS:
            if task.function_placeholder in seen:
                continue
            seen.add(task.function_placeholder)
            functions[task.function_placeholder] = self._create_task_function(
                task,
                source,
            )
        return functions

    def _create_task_function(
        self,
        task: SagaTask,
        source: Path,
    ) -> lambda_.Function:
        construct_name = "".join(part.title() for part in task.task_name.split("-"))
        function_name = f"energyshark-{self.settings.environment}-{task.task_name}"
        role = iam.Role(
            self,
            f"{construct_name}Role",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description=f"Permisos mínimos para {task.task_name}",
        )
        log_group = logs.LogGroup(
            self,
            f"{construct_name}LogGroup",
            log_group_name=f"/aws/lambda/{function_name}",
            retention=self._retention,
            removal_policy=RemovalPolicy.RETAIN,
        )
        log_group.grant_write(role)

        environment = {
            "ENERGYSHARK_ENV": self.settings.environment,
            "SAGA_TASK": task.task_name,
        }
        for secret_kind in task.secret_kinds:
            secret_parameter = self._secret_parameters[secret_kind]
            environment[f"{secret_kind.upper()}_SECRET_ARN"] = (
                secret_parameter.value_as_string
            )
            role.add_to_policy(
                iam.PolicyStatement(
                    actions=["secretsmanager:GetSecretValue"],
                    resources=[secret_parameter.value_as_string],
                )
            )

        function = lambda_.Function(
            self,
            f"{construct_name}Function",
            function_name=function_name,
            description=f"Adaptador de tarea de saga: {task.task_name}",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.X86_64,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset(str(source)),
            role=role,
            log_group=log_group,
            timeout=Duration.seconds(self.settings.lambda_timeout_seconds),
            memory_size=self.settings.lambda_memory_mb,
            environment=environment,
        )
        function.node.add_dependency(log_group)
        return function

    def _create_state_machine(
        self,
        *,
        role: str,
        definition_file: str,
        tasks: tuple[SagaTask, ...],
        timeout_seconds: int,
    ) -> tuple[sfn.CfnStateMachine, sfn.CfnStateMachineAlias]:
        definition_path = (
            Path(__file__).resolve().parents[1] / "asl" / definition_file
        )
        definition = json.loads(definition_path.read_text(encoding="utf-8"))
        definition["TimeoutSeconds"] = timeout_seconds
        definition_string = json.dumps(
            definition,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

        substitutions = {
            task.function_placeholder: self.task_functions[
                task.function_placeholder
            ].function_arn
            for task in tasks
        }
        placeholders = set(re.findall(r"\$\{([A-Za-z0-9]+)\}", definition_string))
        if placeholders != set(substitutions):
            missing = sorted(placeholders - set(substitutions))
            unused = sorted(set(substitutions) - placeholders)
            raise ValueError(
                f"Sustituciones ASL inválidas para {role}: "
                f"faltan={missing}, sobran={unused}"
            )

        construct_name = role.title()
        machine_name = f"energyshark-{self.settings.environment}-{role}-saga"
        execution_role = iam.Role(
            self,
            f"{construct_name}StateMachineRole",
            assumed_by=iam.ServicePrincipal("states.amazonaws.com"),
            description=f"Invocación mínima para la saga {role}",
        )
        execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=["lambda:InvokeFunction"],
                resources=[
                    self.task_functions[task.function_placeholder].function_arn
                    for task in tasks
                ],
            )
        )
        # Estas APIs de entrega de CloudWatch Logs no admiten ARN de recurso.
        execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=list(LOG_DELIVERY_ACTIONS),
                resources=["*"],
            )
        )

        log_group = logs.LogGroup(
            self,
            f"{construct_name}StateMachineLogGroup",
            log_group_name=f"/aws/vendedlogs/states/{machine_name}",
            retention=self._retention,
            removal_policy=RemovalPolicy.RETAIN,
        )
        machine = sfn.CfnStateMachine(
            self,
            f"{construct_name}StateMachine",
            state_machine_name=machine_name,
            state_machine_type="STANDARD",
            role_arn=execution_role.role_arn,
            definition_string=definition_string,
            definition_substitutions=substitutions,
            logging_configuration=sfn.CfnStateMachine.LoggingConfigurationProperty(
                destinations=[
                    sfn.CfnStateMachine.LogDestinationProperty(
                        cloud_watch_logs_log_group=(
                            sfn.CfnStateMachine.CloudWatchLogsLogGroupProperty(
                                log_group_arn=f"{log_group.log_group_arn}:*"
                            )
                        )
                    )
                ],
                include_execution_data=False,
                level="ALL",
            ),
        )
        machine.node.add_dependency(log_group)
        machine.node.add_dependency(execution_role)

        version = sfn.CfnStateMachineVersion(
            self,
            f"{construct_name}StateMachineVersion",
            state_machine_arn=machine.attr_arn,
            state_machine_revision_id=machine.attr_state_machine_revision_id,
            description=f"Versión administrada de la saga {role}",
        )
        alias = sfn.CfnStateMachineAlias(
            self,
            f"{construct_name}StateMachineAlias",
            name="live",
            description=f"Alias estable para la saga {role}",
            state_machine_arn=machine.attr_arn,
            routing_configuration=[
                sfn.CfnStateMachineAlias.RoutingConfigurationVersionProperty(
                    state_machine_version_arn=version.ref,
                    weight=100,
                )
            ],
        )
        alias.node.add_dependency(version)
        return machine, alias

    def _create_connector_role(self) -> iam.Role:
        role = iam.Role(
            self,
            "ConnectorSagaRole",
            role_name=f"energyshark-{self.settings.environment}-connector-sagas",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            description="StartExecution y callbacks de sagas desde Connector EC2",
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["states:StartExecution"],
                resources=[self.seller_alias.ref, self.buyer_alias.ref],
            )
        )
        # SendTaskSuccess/Failure se autoriza mediante task token; AWS no ofrece
        # un ARN de ejecución o máquina utilizable para delimitar estas APIs.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["states:SendTaskSuccess", "states:SendTaskFailure"],
                resources=["*"],
            )
        )
        return role

    @staticmethod
    def _log_retention(days: int) -> logs.RetentionDays:
        supported = {
            1: logs.RetentionDays.ONE_DAY,
            3: logs.RetentionDays.THREE_DAYS,
            5: logs.RetentionDays.FIVE_DAYS,
            7: logs.RetentionDays.ONE_WEEK,
            14: logs.RetentionDays.TWO_WEEKS,
            30: logs.RetentionDays.ONE_MONTH,
            60: logs.RetentionDays.TWO_MONTHS,
            90: logs.RetentionDays.THREE_MONTHS,
        }
        try:
            return supported[days]
        except KeyError as exc:
            raise ValueError(
                f"Retención de logs no soportada: {days}. "
                f"Use uno de {sorted(supported)}"
            ) from exc
