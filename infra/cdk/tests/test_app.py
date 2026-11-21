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

    def test_unmigrated_stack_boundaries_remain_empty(self) -> None:
        for stack in (
            self.stacks.existing,
            self.stacks.monitoring,
        ):
            template = Template.from_stack(stack).to_json()
            self.assertEqual(template.get("Resources", {}), {})

    def test_saga_stack_is_declared_separately(self) -> None:
        template = Template.from_stack(self.stacks.sagas).to_json()
        self.assertTrue(template.get("Resources"))


if __name__ == "__main__":
    unittest.main()
