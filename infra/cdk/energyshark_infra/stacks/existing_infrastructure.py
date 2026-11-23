"""Stack E2 reconstruible: red, computo, frontend y API publica.

Reproduce en una cuenta limpia los recursos que hoy existen hechos a mano:

- VPC con subredes publicas, internet gateway y grupo de seguridad.
- Instancia EC2 con IP elastica, rol IAM minimo y volumen raiz cifrado.
- Bucket privado del frontend servido por CloudFront con OAC.
- API Gateway HTTP con las 29 rutas, autorizador JWT y logs de acceso.

Ningun recurso se importa: el stack crea todo desde cero.
"""

import json

from aws_cdk import (
    CfnOutput,
    Duration,
    Fn,
    RemovalPolicy,
    Stack,
    aws_apigatewayv2 as apigwv2,
    aws_apigatewayv2_authorizers as apigwv2_authorizers,
    aws_apigatewayv2_integrations as apigwv2_integrations,
    aws_certificatemanager as acm,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_logs as logs,
    aws_s3 as s3,
)
from constructs import Construct

from energyshark_infra.api_routes import API_ROUTES
from energyshark_infra.config import DeploymentSettings, log_retention_for


class ExistingInfrastructureStack(Stack):
    """Recursos E2 reproducibles en una cuenta limpia."""

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
            description=(
                "EnergyShark: red, computo, frontend y API publica "
                "reproducibles desde cero"
            ),
            **kwargs,
        )
        self.settings = settings
        config = settings.infrastructure

        self.vpc = self._network(config)
        self.security_group = self._security_group(config)
        self.instance = self._compute(config)
        self.bucket = self._storage(config)
        self.distribution = self._frontend(config)
        self.api = self._api(config)
        self.access_log_group = self._api_stage()

        CfnOutput(self, "ApiUrl", value=self.api.api_endpoint)
        CfnOutput(
            self,
            "FrontendUrl",
            value=f"https://{self.distribution.distribution_domain_name}",
            description="Dominio de CloudFront del frontend",
        )
        CfnOutput(
            self,
            "InstancePublicIp",
            value=self.instance.instance_public_ip,
            description="IP elastica del host de aplicacion",
        )
        CfnOutput(self, "FrontendBucketName", value=self.bucket.bucket_name)

    def _network(self, config) -> ec2.Vpc:
        return ec2.Vpc(
            self,
            "Network",
            ip_addresses=ec2.IpAddresses.cidr(config.network_cidr),
            availability_zones=[
                Fn.select(index, Fn.get_azs()) for index in range(3)
            ],
            nat_gateways=0,
            restrict_default_security_group=True,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="Public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=20,
                )
            ],
        )

    def _security_group(self, config) -> ec2.SecurityGroup:
        group = ec2.SecurityGroup(
            self,
            "ApplicationSecurityGroup",
            vpc=self.vpc,
            description="HTTP, HTTPS y administracion del host EnergyShark",
            allow_all_outbound=True,
        )
        group.add_ingress_rule(
            ec2.Peer.any_ipv4(),
            ec2.Port.tcp(80),
            "HTTP publico redirigido a HTTPS",
        )
        group.add_ingress_rule(
            ec2.Peer.any_ipv4(),
            ec2.Port.tcp(443),
            "HTTPS publico",
        )
        if config.admin_cidr:
            group.add_ingress_rule(
                ec2.Peer.ipv4(config.admin_cidr),
                ec2.Port.tcp(22),
                "SSH administrativo desde el CIDR configurado",
            )
        return group

    def _compute(self, config) -> ec2.Instance:
        role = iam.Role(
            self,
            "ApplicationInstanceRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            description="Rol del host: Session Manager y CloudWatch agent",
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "AmazonSSMManagedInstanceCore"
                ),
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "CloudWatchAgentServerPolicy"
                ),
            ],
        )

        instance = ec2.Instance(
            self,
            "ApplicationInstance",
            vpc=self.vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            instance_type=ec2.InstanceType(config.instance_type),
            machine_image=ec2.MachineImage.generic_linux(config.ami_ids),
            key_pair=ec2.KeyPair.from_key_pair_name(
                self,
                "ApplicationKeyPair",
                config.key_name,
            ),
            security_group=self.security_group,
            role=role,
            require_imdsv2=True,
            block_devices=[
                ec2.BlockDevice(
                    device_name="/dev/sda1",
                    volume=ec2.BlockDeviceVolume.ebs(
                        20,
                        encrypted=True,
                        volume_type=ec2.EbsDeviceVolumeType.GP3,
                    ),
                )
            ],
            user_data=self._bootstrap_user_data(config),
        )
        instance.node.add_dependency(self.security_group)

        address = ec2.CfnEIP(self, "ApplicationElasticIp", domain="vpc")
        ec2.CfnEIPAssociation(
            self,
            "ApplicationElasticIpAssociation",
            allocation_id=address.attr_allocation_id,
            instance_id=instance.instance_id,
        )
        return instance

    def _bootstrap_user_data(self, config) -> ec2.UserData:
        user_data = ec2.UserData.for_linux()
        user_data.add_commands(
            "set -euo pipefail",
            "exec > /var/log/energyshark-bootstrap.log 2>&1",
            "export DEBIAN_FRONTEND=noninteractive",
            "apt-get update -y",
            "apt-get install -y docker.io docker-compose-v2 git",
            "systemctl enable --now docker",
        )
        if config.app_repository_url:
            user_data.add_commands(
                "mkdir -p /opt/energyshark",
                f"git clone --depth 1 {config.app_repository_url} /opt/energyshark",
                "cd /opt/energyshark",
                "docker compose -f docker-compose.prod.yml up -d",
            )
        else:
            user_data.add_commands(
                "echo 'appRepositoryUrl vacio: host sin aplicacion desplegada'",
            )
        return user_data

    def _storage(self, config) -> s3.Bucket:
        return s3.Bucket(
            self,
            "FrontendBucket",
            bucket_name=config.frontend_bucket_name,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            bucket_key_enabled=True,
            public_read_access=False,
            versioned=False,
            removal_policy=(
                RemovalPolicy.RETAIN
                if self.settings.is_production
                else RemovalPolicy.DESTROY
            ),
            auto_delete_objects=not self.settings.is_production,
        )

    def _frontend(self, config) -> cloudfront.Distribution:
        domain_names = (
            [config.frontend_domain_name] if config.frontend_domain_name else None
        )
        certificate = (
            acm.Certificate.from_certificate_arn(
                self,
                "FrontendCertificate",
                config.certificate_arn,
            )
            if config.certificate_arn
            else None
        )
        return cloudfront.Distribution(
            self,
            "FrontendDistribution",
            comment="Frontend Energyshark",
            default_root_object="index.html",
            price_class=cloudfront.PriceClass.PRICE_CLASS_ALL,
            http_version=cloudfront.HttpVersion.HTTP2,
            domain_names=domain_names,
            certificate=certificate,
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(
                    self.bucket
                ),
                viewer_protocol_policy=(
                    cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS
                ),
                allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD,
                cached_methods=cloudfront.CachedMethods.CACHE_GET_HEAD,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                compress=True,
            ),
            error_responses=[
                cloudfront.ErrorResponse(
                    http_status=403,
                    response_http_status=200,
                    response_page_path="/index.html",
                    ttl=Duration.seconds(0),
                ),
                cloudfront.ErrorResponse(
                    http_status=404,
                    response_http_status=200,
                    response_page_path="/index.html",
                    ttl=Duration.seconds(0),
                ),
            ],
        )

    def _api(self, config) -> apigwv2.HttpApi:
        api = apigwv2.HttpApi(
            self,
            "PublicApi",
            api_name=f"energysharkg10-{self.settings.environment}-api",
            description="API publica EnergyShark",
            create_default_stage=False,
            cors_preflight=apigwv2.CorsPreflightOptions(
                allow_origins=list(config.cors_origins),
                allow_methods=[
                    apigwv2.CorsHttpMethod.GET,
                    apigwv2.CorsHttpMethod.POST,
                    apigwv2.CorsHttpMethod.PATCH,
                    apigwv2.CorsHttpMethod.OPTIONS,
                ],
                allow_headers=["authorization", "content-type"],
                max_age=Duration.seconds(300),
            ),
        )
        authorizer = apigwv2_authorizers.HttpJwtAuthorizer(
            "Auth0JwtAuthorizer",
            config.jwt_issuer,
            jwt_audience=[config.jwt_audience],
            identity_source=["$request.header.Authorization"],
        )
        for route in API_ROUTES:
            api.add_routes(
                path=route.path,
                methods=[apigwv2.HttpMethod(route.method)],
                integration=apigwv2_integrations.HttpUrlIntegration(
                    f"Backend{route.method}{route.path}",
                    f"{config.api_backend_url}{route.backend_path}",
                    method=apigwv2.HttpMethod(route.method),
                ),
                authorizer=authorizer if route.require_auth else None,
            )
        return api

    def _api_stage(self) -> logs.LogGroup:
        log_group = logs.LogGroup(
            self,
            "ApiAccessLogs",
            retention=log_retention_for(self.settings.log_retention_days),
            removal_policy=RemovalPolicy.DESTROY,
        )
        apigwv2.CfnStage(
            self,
            "DefaultStage",
            api_id=self.api.api_id,
            stage_name="$default",
            auto_deploy=True,
            access_log_settings=apigwv2.CfnStage.AccessLogSettingsProperty(
                destination_arn=log_group.log_group_arn,
                format=json.dumps(
                    {
                        "requestId": "$context.requestId",
                        "ip": "$context.identity.sourceIp",
                        "method": "$context.httpMethod",
                        "path": "$context.path",
                        "status": "$context.status",
                        "responseLength": "$context.responseLength",
                        "integrationStatus": "$context.integrationStatus",
                        "latency": "$context.responseLatency",
                    }
                ),
            ),
        )
        return log_group
