import sys
import unittest
from types import ModuleType
from unittest.mock import Mock, patch

from meal_management.delivery import SESDelivery
from meal_management.email_queue import EmailPreview
from meal_management.errors import DependencyError
from meal_management.security import generate_token


class SESClientConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.client.send_raw_email.return_value = {"MessageId": "fictional-message-id"}
        self.config = Mock()
        self.config_factory = Mock(return_value=self.config)
        self.boto3 = ModuleType("boto3")
        self.boto3.client = Mock(return_value=self.client)
        self.botocore = ModuleType("botocore")
        self.botocore.__path__ = []
        self.config_module = ModuleType("botocore.config")
        self.config_module.Config = self.config_factory
        self.modules = {
            "boto3": self.boto3,
            "botocore": self.botocore,
            "botocore.config": self.config_module,
        }
        self.adapter = SESDelivery("meals@workplace.co.uk", "ap-south-1", True)
        self.preview = EmailPreview(7, "recipient@gmail.com", "Fictional Employee", generate_token(), "")

    def test_client_disables_sdk_retries_and_bounds_transport_timeouts(self):
        with patch.dict(sys.modules, self.modules):
            result = self.adapter.send(self.preview, allow_real_email=True)
            self.assertIs(self.adapter._get_client(), self.client)
        self.assertEqual(result, "fictional-message-id")
        self.config_factory.assert_called_once_with(
            retries={"total_max_attempts": 1}, connect_timeout=10, read_timeout=20,
        )
        self.boto3.client.assert_called_once_with("ses", region_name="ap-south-1", config=self.config)
        self.client.send_raw_email.assert_called_once()

    def test_uncertain_client_failure_does_not_trigger_another_send(self):
        self.client.send_raw_email.side_effect = TimeoutError("fictional response timeout")
        with patch.dict(sys.modules, self.modules):
            with self.assertRaises(TimeoutError):
                self.adapter.send(self.preview, allow_real_email=True)
        self.client.send_raw_email.assert_called_once()
        self.config_factory.assert_called_once_with(
            retries={"total_max_attempts": 1}, connect_timeout=10, read_timeout=20,
        )

    def test_optional_sdk_absence_raises_safe_dependency_error(self):
        with patch.dict(sys.modules, {"boto3": None, "botocore": None, "botocore.config": None}):
            with self.assertRaisesRegex(DependencyError, "BOTO3_NOT_INSTALLED"):
                self.adapter._get_client()


if __name__ == "__main__":
    unittest.main()
