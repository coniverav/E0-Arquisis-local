import json
import re
from dataclasses import dataclass
from pathlib import Path

from aws_cdk import (
    ArnFormat,
    CfnOutput,
    CfnParameter,
    Duration,
    RemovalPolicy,
    Stack,
    aws_ecr_assets as ecr_assets,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_stepfunctions as sfn,
)
from constructs import Construct

from energyshark_infra.config import DeploymentSettings, log_retention_for


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
        ("database",),
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
        ("database",),
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
        ("database",),
    ),
)

BUYER_TASKS = (
    SagaTask(
        "validate-buyer-token",
        "ValidateBuyerTokenFunctionArn",
        ("database", "wallet"),
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
        self._retention = log_retention_for(settings.log_retention_days)
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
            roles=[self.connector_saga_role.role_name],
        )
        self.connector_instance_profile.node.add_dependency(
            self.connector_saga_role
        )

        CfnOutput(self, "SellerSagaArn", value=self.seller_state_machine.attr_arn)
        CfnOutput(self, "BuyerSagaArn", value=self.buyer_state_machine.attr_arn)
        for output_id, function in self.task_functions.items():
            CfnOutput(self, output_id, value=function.function_arn)

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
            "ConnectorSagaInstanceProfileArn",
            value=self.connector_instance_profile.attr_arn,
            description="Instance profile para asociar al host de Connector",
        )

    def _create_secret_parameters(self) -> dict[str, CfnParameter]:
        # El parámetro termina en Resource de IAM y debe identificar un único secreto.
        secret_arn = r"arn:[a-z0-9-]+:secretsmanager:[a-z0-9-]+:[0-9]{12}:secret:[A-Za-z0-9/_+=.@-]+-[A-Za-z0-9]{6}"
        return {
            "database": CfnParameter(
                self,
                "DatabaseSecretArn",
                type="String",
                no_echo=True,
                allowed_pattern=secret_arn,
                description="ARN de Secrets Manager con acceso a PostgreSQL",
            ),
            "wallet": CfnParameter(
                self,
                "WalletSecretArn",
                type="String",
                no_echo=True,
                allowed_pattern=secret_arn,
                description="ARN de Secrets Manager con configuración Wallet",
            ),
        }

    def _create_task_functions(self) -> dict[str, lambda_.DockerImageFunction]:
        source = Path(__file__).resolve().parents[2] / "lambda_src" / "saga_task"
        functions: dict[str, lambda_.DockerImageFunction] = {}
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
    ) -> lambda_.DockerImageFunction:
        construct_name = "".join(part.title() for part in task.task_name.split("-"))
        role = iam.Role(
            self,
            f"{construct_name}Role",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description=f"Permisos mínimos para {task.task_name}",
        )
        log_group = logs.LogGroup(
            self,
            f"{construct_name}LogGroup",
            retention=self._retention,
            removal_policy=RemovalPolicy.RETAIN,
        )
        log_group.grant_write(role)

        environment = {
            "ENERGYSHARK_ENV": self.settings.environment,
            "CITY_ID": "KLD",
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

        if task.task_name == "seller-proposal":
            # Estas API sólo aceptan Resource:*; el task token es la capacidad
            # efímera que delimita qué espera puede completar la Lambda.
            role.add_to_policy(
                iam.PolicyStatement(
                    actions=["states:SendTaskSuccess"],
                    resources=["*"],
                )
            )

        function = lambda_.DockerImageFunction(
            self,
            f"{construct_name}Function",
            description=f"Adaptador de tarea de saga: {task.task_name}",
            architecture=lambda_.Architecture.X86_64,
            code=lambda_.DockerImageCode.from_image_asset(
                str(source), platform=ecr_assets.Platform.LINUX_AMD64,
            ),
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
        log_delivery = execution_role.add_to_principal_policy(
            iam.PolicyStatement(
                actions=list(LOG_DELIVERY_ACTIONS),
                resources=["*"],
            )
        )

        log_group = logs.LogGroup(
            self,
            f"{construct_name}StateMachineLogGroup",
            retention=self._retention,
            removal_policy=RemovalPolicy.RETAIN,
        )
        machine = sfn.CfnStateMachine(
            self,
            f"{construct_name}StateMachine",
            state_machine_type="STANDARD",
            encryption_configuration=sfn.CfnStateMachine.EncryptionConfigurationProperty(
                type="AWS_OWNED_KEY",
            ),
            role_arn=execution_role.role_arn,
            definition_string=definition_string,
            definition_substitutions=substitutions,
            logging_configuration=sfn.CfnStateMachine.LoggingConfigurationProperty(
                destinations=[
                    sfn.CfnStateMachine.LogDestinationProperty(
                        cloud_watch_logs_log_group=(
                            sfn.CfnStateMachine.CloudWatchLogsLogGroupProperty(
                                log_group_arn=log_group.log_group_arn
                            )
                        )
                    )
                ],
                include_execution_data=False,
                level="ALL",
            ),
        )
        machine.node.add_dependency(log_group)
        machine.node.add_dependency(log_delivery.policy_dependable)

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
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            description="StartExecution y callbacks de sagas desde Connector EC2",
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["states:StartExecution"],
                resources=[self.seller_alias.ref, self.buyer_alias.ref],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["states:DescribeExecution"],
                resources=[
                    self.format_arn(
                        service="states",
                        resource="execution",
                        resource_name=f"{machine.attr_name}:*",
                        arn_format=ArnFormat.COLON_RESOURCE_NAME,
                    )
                    for machine in (self.seller_state_machine, self.buyer_state_machine)
                ],
            )
        )
        # SendTaskSuccess se autoriza mediante task token; AWS no ofrece
        # un ARN de ejecución o máquina utilizable para delimitar estas APIs.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["states:SendTaskSuccess"],
                resources=["*"],
            )
        )
        return role
