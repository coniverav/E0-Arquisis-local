"""E3-076: el stack E2 se declara completo y reproducible."""

import json
import unittest

from aws_cdk.assertions import Template

from app import build_app
from energyshark_infra.api_routes import API_ROUTES


CONTEXT = {
    "environment": "dev",
    "environments": {
        "dev": {
            "stackPrefix": "EnergySharkDev",
            "logRetentionDays": 7,
        }
    },
}

PROD_CONTEXT = {
    "environment": "prod",
    "environments": {
        "prod": {
            "stackPrefix": "EnergySharkProd",
            "logRetentionDays": 30,
            "frontendDomainName": "app.energyshark-g10.tech",
            "certificateArn": (
                "arn:aws:acm:us-east-1:111122223333:certificate/"
                "00000000-0000-0000-0000-000000000000"
            ),
            "adminCidr": "203.0.113.0/24",
            "alarmEmail": "ops@example.com",
        }
    },
}


class ReproducibleStackTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app, self.stacks = build_app(CONTEXT)
        self.template = Template.from_stack(self.stacks.existing).to_json()
        self.resources = self.template["Resources"]

    def resources_of(self, resource_type: str) -> dict:
        return {
            logical_id: resource
            for logical_id, resource in self.resources.items()
            if resource["Type"] == resource_type
        }

    def single(self, resource_type: str) -> dict:
        found = self.resources_of(resource_type)
        self.assertEqual(len(found), 1, f"se esperaba un {resource_type}")
        return next(iter(found.values()))["Properties"]

    def test_network_is_public_and_has_no_nat(self) -> None:
        self.assertEqual(len(self.resources_of("AWS::EC2::VPC")), 1)
        self.assertEqual(len(self.resources_of("AWS::EC2::InternetGateway")), 1)
        self.assertEqual(len(self.resources_of("AWS::EC2::NatGateway")), 0)

        subnets = self.resources_of("AWS::EC2::Subnet")
        self.assertEqual(len(subnets), 3)
        for subnet in subnets.values():
            self.assertTrue(subnet["Properties"]["MapPublicIpOnLaunch"])

    def test_security_group_exposes_web_ports(self) -> None:
        group = self.single("AWS::EC2::SecurityGroup")
        ports = {
            rule.get("FromPort")
            for rule in group["SecurityGroupIngress"]
        }
        self.assertIn(80, ports)
        self.assertIn(443, ports)
        self.assertNotIn(22, ports)
        for rule in group["SecurityGroupIngress"]:
            if rule.get("FromPort") == 80:
                self.assertEqual(rule["CidrIp"], "0.0.0.0/0")

    def test_compute_has_elastic_ip_encryption_and_ssm_role(self) -> None:
        self.assertEqual(len(self.resources_of("AWS::EC2::Instance")), 1)
        self.assertEqual(len(self.resources_of("AWS::EC2::EIP")), 1)
        self.assertEqual(len(self.resources_of("AWS::EC2::EIPAssociation")), 1)

        instance = self.single("AWS::EC2::Instance")
        self.assertEqual(instance["InstanceType"], "t3.micro")
        self.assertEqual(instance["KeyName"], "arquisisg10")
        root = instance["BlockDeviceMappings"][0]["Ebs"]
        self.assertTrue(root["Encrypted"])
        self.assertEqual(root["VolumeSize"], 20)

        launch_template = self.single("AWS::EC2::LaunchTemplate")
        self.assertEqual(
            launch_template["LaunchTemplateData"]["MetadataOptions"]["HttpTokens"],
            "required",
        )

        roles = self.resources_of("AWS::IAM::Role")
        policies = json.dumps(roles)
        self.assertIn("AmazonSSMManagedInstanceCore", policies)
        self.assertIn("CloudWatchAgentServerPolicy", policies)

    def test_bucket_is_private_encrypted_and_tls_only(self) -> None:
        bucket = self.single("AWS::S3::Bucket")
        self.assertEqual(
            bucket["PublicAccessBlockConfiguration"],
            {
                "BlockPublicAcls": True,
                "BlockPublicPolicy": True,
                "IgnorePublicAcls": True,
                "RestrictPublicBuckets": True,
            },
        )
        rule = bucket["BucketEncryption"]["ServerSideEncryptionConfiguration"][0]
        self.assertEqual(
            rule["ServerSideEncryptionByDefault"]["SSEAlgorithm"],
            "AES256",
        )
        self.assertTrue(rule["BucketKeyEnabled"])

        policy = json.dumps(self.single("AWS::S3::BucketPolicy"))
        self.assertIn("aws:SecureTransport", policy)
        self.assertIn("false", policy)

    def test_cloudfront_serves_spa_through_origin_access_control(self) -> None:
        self.assertEqual(
            len(self.resources_of("AWS::CloudFront::OriginAccessControl")), 1
        )
        config = self.single("AWS::CloudFront::Distribution")["DistributionConfig"]
        self.assertEqual(config["DefaultRootObject"], "index.html")
        self.assertNotIn("ViewerCertificate", config)

        origin = config["Origins"][0]
        self.assertIn("OriginAccessControlId", origin)
        self.assertEqual(origin["S3OriginConfig"]["OriginAccessIdentity"], "")

        behavior = config["DefaultCacheBehavior"]
        self.assertEqual(behavior["ViewerProtocolPolicy"], "redirect-to-https")

        errors = {
            item["ErrorCode"]: item
            for item in config["CustomErrorResponses"]
        }
        for code in (403, 404):
            self.assertEqual(str(errors[code]["ResponseCode"]), "200")
            self.assertEqual(errors[code]["ResponsePagePath"], "/index.html")

    def test_api_declares_every_route_and_jwt_authorizer(self) -> None:
        routes = {
            route["Properties"]["RouteKey"]: route["Properties"]
            for route in self.resources_of("AWS::ApiGatewayV2::Route").values()
        }
        self.assertEqual(len(routes), len(API_ROUTES))

        protected = [
            route for route in routes.values()
            if route["AuthorizationType"] == "JWT"
        ]
        self.assertEqual(
            len(protected),
            len([route for route in API_ROUTES if route.require_auth]),
        )
        for route in protected:
            self.assertIn("AuthorizerId", route)

        authorizer = self.single("AWS::ApiGatewayV2::Authorizer")
        self.assertEqual(authorizer["AuthorizerType"], "JWT")
        self.assertEqual(authorizer["IdentitySource"], ["$request.header.Authorization"])

        api = self.single("AWS::ApiGatewayV2::Api")
        self.assertEqual(api["ProtocolType"], "HTTP")
        self.assertIn("CorsConfiguration", api)

    def test_api_integrations_point_to_the_backend_prefix(self) -> None:
        integrations = self.resources_of("AWS::ApiGatewayV2::Integration")
        self.assertEqual(len(integrations), len(API_ROUTES))
        expected = {
            f"https://energyshark-g10.tech{route.backend_path}"
            for route in API_ROUTES
        }
        actual = {
            item["Properties"]["IntegrationUri"]
            for item in integrations.values()
        }
        self.assertEqual(actual, expected)

    def test_api_stage_auto_deploys_with_access_logs(self) -> None:
        stage = self.single("AWS::ApiGatewayV2::Stage")
        self.assertEqual(stage["StageName"], "$default")
        self.assertTrue(stage["AutoDeploy"])
        self.assertIn("AccessLogSettings", stage)
        format_fields = json.loads(stage["AccessLogSettings"]["Format"])
        self.assertIn("requestId", format_fields)
        self.assertEqual(len(self.resources_of("AWS::Logs::LogGroup")), 1)

    def test_monitoring_declares_alarms_topic_and_dashboard(self) -> None:
        template = Template.from_stack(self.stacks.monitoring).to_json()
        resources = template["Resources"]
        alarms = [
            resource for resource in resources.values()
            if resource["Type"] == "AWS::CloudWatch::Alarm"
        ]
        self.assertEqual(len(alarms), 5)
        for alarm in alarms:
            self.assertTrue(alarm["Properties"]["AlarmActions"])
        self.assertEqual(
            len(
                [
                    resource for resource in resources.values()
                    if resource["Type"] == "AWS::SNS::Topic"
                ]
            ),
            1,
        )
        self.assertEqual(
            len(
                [
                    resource for resource in resources.values()
                    if resource["Type"] == "AWS::CloudWatch::Dashboard"
                ]
            ),
            1,
        )
        self.assertNotIn(
            "AWS::SNS::Subscription",
            {resource["Type"] for resource in resources.values()},
        )

    def test_production_tightens_retention_and_domain(self) -> None:
        app, stacks = build_app(PROD_CONTEXT)
        existing = Template.from_stack(stacks.existing).to_json()
        config = next(
            resource["Properties"]["DistributionConfig"]
            for resource in existing["Resources"].values()
            if resource["Type"] == "AWS::CloudFront::Distribution"
        )
        self.assertEqual(config["Aliases"], ["app.energyshark-g10.tech"])
        self.assertIn("ViewerCertificate", config)

        security_group = next(
            resource["Properties"]
            for resource in existing["Resources"].values()
            if resource["Type"] == "AWS::EC2::SecurityGroup"
        )
        self.assertIn(
            22,
            {rule.get("FromPort") for rule in security_group["SecurityGroupIngress"]},
        )

        monitoring = Template.from_stack(stacks.monitoring).to_json()
        subscriptions = [
            resource for resource in monitoring["Resources"].values()
            if resource["Type"] == "AWS::SNS::Subscription"
        ]
        self.assertEqual(len(subscriptions), 1)
        self.assertEqual(subscriptions[0]["Properties"]["Protocol"], "email")

        bucket = next(
            resource
            for resource in existing["Resources"].values()
            if resource["Type"] == "AWS::S3::Bucket"
        )
        self.assertEqual(bucket["DeletionPolicy"], "Retain")

    def test_synthesis_is_deterministic(self) -> None:
        _, other = build_app(CONTEXT)
        self.assertEqual(
            Template.from_stack(self.stacks.existing).to_json(),
            Template.from_stack(other.existing).to_json(),
        )


if __name__ == "__main__":
    unittest.main()
