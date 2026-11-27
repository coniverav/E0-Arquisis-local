"""Red privada E3-055/056 sin efectuar conexiones externas."""

from __future__ import annotations

import os
import sys
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

import boto3


CDK_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = CDK_ROOT.parents[1]
LAMBDA_SOURCE = CDK_ROOT / "lambda_src" / "saga_task"
sys.path.insert(0, str(LAMBDA_SOURCE))

import buyer_validation  # noqa: E402
import wallet_client  # noqa: E402


PROXY_URL = "http://10.0.1.25:3128"


class ProxyAwareClientsTest(unittest.TestCase):
    def proxy_environment(self):
        return patch.dict(
            os.environ,
            {
                "HTTPS_PROXY": PROXY_URL,
                "https_proxy": PROXY_URL,
                "NO_PROXY": "169.254.169.254,10.0.1.25",
                "AWS_ACCESS_KEY_ID": "unit-test-access-key",
                "AWS_SECRET_ACCESS_KEY": "unit-test-secret-key",
                "AWS_SESSION_TOKEN": "unit-test-session-token",
                "AWS_EC2_METADATA_DISABLED": "true",
                "AWS_DEFAULT_REGION": "us-east-1",
            },
            clear=False,
        )

    def test_boto3_secrets_manager_and_stepfunctions_use_https_proxy(self) -> None:
        with self.proxy_environment():
            for service in ("secretsmanager", "stepfunctions"):
                client = boto3.client(service, region_name="us-east-1")
                proxies = client._endpoint.http_session._proxy_config._proxies
                self.assertEqual(proxies.get("https"), PROXY_URL)

    def test_wallet_and_jwks_use_proxy_aware_stdlib_urlopen(self) -> None:
        with self.proxy_environment():
            handler = urllib.request.ProxyHandler()
            self.assertEqual(handler.proxies.get("https"), PROXY_URL)
            self.assertIs(wallet_client.urlopen, urllib.request.urlopen)
            self.assertIs(buyer_validation.urlopen, urllib.request.urlopen)


class PrivateProxyConfigurationTest(unittest.TestCase):
    def test_squid_is_connect_only_and_has_an_exact_allowlist(self) -> None:
        config = (
            REPOSITORY_ROOT / "infra" / "proxy" / "squid-sagas.conf"
        ).read_text(encoding="utf-8")
        for destination in (
            "wallet.iic2173.org",
            "secretsmanager.us-east-1.amazonaws.com",
            "states.us-east-1.amazonaws.com",
        ):
            self.assertIn(f"dstdomain {destination}", config)
        self.assertIn("acl CONNECT method CONNECT", config)
        self.assertIn("acl TLS_port port 443", config)
        self.assertIn("http_access deny !CONNECT", config)
        self.assertIn("http_access deny !TLS_port", config)
        self.assertIn("http_access deny all", config)
        self.assertNotIn("http_access allow all", config)
        self.assertIn("access_log none", config)

    def test_compose_binds_proxies_to_configured_private_ip(self) -> None:
        compose = (REPOSITORY_ROOT / "docker-compose.prod.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            '"${E3_EC2_PRIVATE_IP:-127.0.0.1}:${E3_POSTGRES_PROXY_PORT:-15432}:15432"',
            compose,
        )
        self.assertIn(
            '"${E3_EC2_PRIVATE_IP:-127.0.0.1}:${E3_HTTPS_PROXY_PORT:-3128}:3128"',
            compose,
        )
        self.assertNotIn('"0.0.0.0:15432:15432"', compose)
        self.assertNotIn('"0.0.0.0:3128:3128"', compose)
        self.assertIn('"127.0.0.1:5433:5432"', compose)

    def test_saga_flags_default_off_in_master_replicas_and_scheduler(self) -> None:
        for name in ("docker-compose.yml", "docker-compose.prod.yml"):
            compose = (REPOSITORY_ROOT / name).read_text(encoding="utf-8")
            self.assertEqual(
                compose.count(
                    "E3_SELLER_SAGA_ENABLED: ${E3_SELLER_SAGA_ENABLED:-false}"
                ),
                3,
            )
            self.assertEqual(
                compose.count(
                    "E3_BUYER_SAGA_ENABLED: ${E3_BUYER_SAGA_ENABLED:-false}"
                ),
                3,
            )

    def test_connector_uses_instance_profile_without_static_keys(self) -> None:
        for name in ("docker-compose.yml", "docker-compose.prod.yml"):
            compose = (REPOSITORY_ROOT / name).read_text(encoding="utf-8")
            connector = compose.split("  connector:", 1)[1]
            self.assertIn("AWS_EC2_METADATA_SERVICE_ENDPOINT_MODE: IPv4", connector)
            self.assertIn("NO_PROXY: ${NO_PROXY:-169.254.169.254}", connector)
            self.assertNotIn("AWS_ACCESS_KEY_ID:", connector)
            self.assertNotIn("AWS_SECRET_ACCESS_KEY:", connector)


if __name__ == "__main__":
    unittest.main()
