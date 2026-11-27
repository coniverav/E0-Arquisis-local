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

    def test_every_stack_boundary_declares_resources(self) -> None:
        for stack in (
            self.stacks.existing,
            self.stacks.sagas,
            self.stacks.monitoring,
        ):
            template = Template.from_stack(stack).to_json()
            self.assertTrue(
                template.get("Resources", {}),
                f"{stack.stack_name} no declara recursos",
            )

    def test_saga_stack_is_declared_separately(self) -> None:
        template = Template.from_stack(self.stacks.sagas).to_json()
        self.assertTrue(template.get("Resources"))

    def test_sagas_only_scope_does_not_construct_other_stacks(self) -> None:
        app, stacks = build_app(
            {
                "environment": "prod",
                "deploymentScope": "sagas",
                "environments": {
                    "prod": {"stackPrefix": "EnergySharkProd"},
                },
            }
        )
        self.assertIsNone(stacks.existing)
        self.assertIsNone(stacks.monitoring)
        self.assertEqual(stacks.sagas.stack_name, "EnergySharkProd-Sagas")
        assembly = app.synth()
        self.assertEqual(
            [artifact.stack_name for artifact in assembly.stacks],
            ["EnergySharkProd-Sagas"],
        )


if __name__ == "__main__":
    unittest.main()
