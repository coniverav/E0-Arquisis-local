import unittest

from aws_cdk.assertions import Template

from app import build_app


class InfrastructureBaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app, self.stacks = build_app(
            {
                "environment": "dev",
                "environments": {
                    "dev": {"stackPrefix": "EnergySharkDev"},
                },
            }
        )

    def test_defines_expected_stack_boundaries(self) -> None:
        self.assertEqual(
            self.stacks.existing.stack_name,
            "EnergySharkDev-ExistingInfrastructure",
        )
        self.assertEqual(self.stacks.sagas.stack_name, "EnergySharkDev-Sagas")
        self.assertEqual(
            self.stacks.monitoring.stack_name,
            "EnergySharkDev-Monitoring",
        )

    def test_base_does_not_create_aws_resources(self) -> None:
        for stack in (
            self.stacks.existing,
            self.stacks.sagas,
            self.stacks.monitoring,
        ):
            template = Template.from_stack(stack).to_json()
            self.assertEqual(template.get("Resources", {}), {})


if __name__ == "__main__":
    unittest.main()
