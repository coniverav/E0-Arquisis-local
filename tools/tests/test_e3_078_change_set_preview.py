"""E3-078: contratos de la vista previa de change sets."""

import importlib.util
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "e3_078_change_set_preview.py"
spec = importlib.util.spec_from_file_location(
    "e3_078_change_set_preview", MODULE_PATH
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class StackMissing(Exception):
    pass


class FakeCloudFormation:
    """Cliente falso: registra llamadas y falla si se intenta ejecutar el change set."""

    def __init__(self, stack_exists: bool = False, status: str = "CREATE_COMPLETE"):
        self.calls = []
        self.stack_exists = stack_exists
        self.change_set_status = status
        self.stack_status = "CREATE_COMPLETE"

    def describe_stacks(self, **kwargs):
        self.calls.append(("describe_stacks", kwargs))
        if not self.stack_exists:
            raise StackMissing("ValidationError: Stack does not exist")
        return {"Stacks": [{"StackStatus": self.stack_status}]}

    def create_change_set(self, **kwargs):
        self.calls.append(("create_change_set", kwargs))
        return {"Id": "change-set-id"}

    def describe_change_set(self, **kwargs):
        self.calls.append(("describe_change_set", kwargs))
        return {
            "Status": self.change_set_status,
            "Changes": [
                {
                    "Type": "Resource",
                    "ResourceChange": {
                        "Action": "Create",
                        "LogicalResourceId": "Bucket",
                        "ResourceType": "AWS::S3::Bucket",
                    },
                }
            ],
        }

    def delete_change_set(self, **kwargs):
        self.calls.append(("delete_change_set", kwargs))

    def delete_stack(self, **kwargs):
        self.calls.append(("delete_stack", kwargs))


class PreviewTest(unittest.TestCase):
    def test_create_preview_never_executes_and_cleans_up(self) -> None:
        client = FakeCloudFormation(stack_exists=False)

        result = module.preview(
            client,
            stack_name="e3078-preview",
            change_set_name="e3078-preview-cs",
            template_body='{"Resources":{}}',
            poll_s=0,
        )

        names = [name for name, _ in client.calls]
        self.assertIn("create_change_set", names)
        self.assertIn("delete_change_set", names)
        self.assertIn("delete_stack", names)
        self.assertNotIn("execute_change_set", names)
        self.assertEqual(len(result.changes), 1)
        self.assertTrue(result.stack_created)
        self.assertEqual(result.summary()["by_action"], {"Create": 1})

    def test_refuses_to_touch_existing_stack_on_create(self) -> None:
        client = FakeCloudFormation(stack_exists=True)

        with self.assertRaises(SystemExit):
            module.preview(
                client,
                stack_name="produccion",
                change_set_name="cs",
                template_body='{"Resources":{}}',
                poll_s=0,
            )

        self.assertEqual([name for name, _ in client.calls], ["describe_stacks"])

    def test_update_preview_does_not_delete_stack(self) -> None:
        client = FakeCloudFormation(stack_exists=True)

        result = module.preview(
            client,
            stack_name="existente",
            change_set_name="cs",
            change_set_type="UPDATE",
            template_body='{"Resources":{}}',
            poll_s=0,
        )

        names = [name for name, _ in client.calls]
        self.assertIn("create_change_set", names)
        self.assertIn("delete_change_set", names)
        self.assertNotIn("delete_stack", names)
        self.assertFalse(result.stack_created)

    def test_strips_bootstrap_rule_only(self) -> None:
        template = {
            "Parameters": {
                "BootstrapVersion": {"Type": "AWS::SSM::Parameter::Value<String>"},
                "Otro": {"Type": "String"},
            },
            "Rules": {
                "CheckBootstrapVersion": {
                    "Assertions": [
                        {
                            "Assert": {
                                "Fn::Not": [
                                    {"Fn::Contains": [["1", "2"], {"Ref": "BootstrapVersion"}]}
                                ]
                            }
                        }
                    ]
                }
            },
            "Resources": {"Bucket": {}},
        }

        cleaned = module.strip_bootstrap_rule(template)

        self.assertNotIn("BootstrapVersion", cleaned["Parameters"])
        self.assertIn("Otro", cleaned["Parameters"])
        self.assertNotIn("Rules", cleaned)
        self.assertIn("Resources", cleaned)
        self.assertIn("BootstrapVersion", template["Parameters"])

    def test_rejects_template_body_over_limit(self) -> None:
        client = FakeCloudFormation()
        with self.assertRaises(ValueError):
            module.preview(
                client,
                stack_name="x",
                change_set_name="cs",
                template_body=None,
                template_url=None,
                poll_s=0,
            )

    def test_upload_uses_existing_bucket_and_returns_url(self) -> None:
        class FakeS3:
            def __init__(self):
                self.calls = []

            def put_object(self, **kwargs):
                self.calls.append(("put_object", kwargs))

            def delete_object(self, **kwargs):
                self.calls.append(("delete_object", kwargs))

        s3 = FakeS3()
        key = module.template_object_key("stack", timestamp=1)
        url = module.upload_template(s3, "bucket-existente", key, '{"Resources":{}}')
        module.delete_template(s3, "bucket-existente", key)

        self.assertEqual(key, "e3-078/stack-1.json")
        self.assertIn("bucket-existente", url)
        self.assertIn(key, url)
        put = s3.calls[0][1]
        self.assertEqual(put["Bucket"], "bucket-existente")
        self.assertEqual(put["ServerSideEncryption"], "AES256")
        self.assertEqual(put["Body"], b'{"Resources":{}}')
        self.assertEqual(s3.calls[1][0], "delete_object")


if __name__ == "__main__":
    unittest.main()
