import base64
import unittest

from meal_management.config import Settings
from meal_management.errors import ConfigurationError


class SettingsTests(unittest.TestCase):
    def environment(self):
        return {
            "DB_HOST": "localhost",
            "DB_PORT": "3306",
            "DB_NAME": "meal_management",
            "DB_USER": "meal_user",
            "DB_PASSWORD": "unit-test-private-password",
            "QR_ENCRYPTION_KEYS": base64.urlsafe_b64encode(b"k" * 32).decode(),
        }

    def test_settings_load_from_explicit_environment(self):
        settings = Settings.from_env(self.environment())
        self.assertIsInstance(settings, Settings)

    def test_settings_repr_does_not_expose_secrets(self):
        environment = self.environment()
        settings = Settings.from_env(environment)
        self.assertNotIn(environment["DB_PASSWORD"], repr(settings))
        self.assertNotIn(environment["QR_ENCRYPTION_KEYS"], repr(settings))

    def test_missing_required_configuration_is_rejected(self):
        for name in ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD", "QR_ENCRYPTION_KEYS"):
            with self.subTest(name=name):
                environment = self.environment()
                environment.pop(name)
                with self.assertRaises(ConfigurationError):
                    Settings.from_env(environment)

    def test_invalid_port_is_rejected(self):
        for port in ("zero", "0", "65536", "-1"):
            with self.subTest(port=port):
                environment = self.environment()
                environment["DB_PORT"] = port
                with self.assertRaises(ConfigurationError):
                    Settings.from_env(environment)

    def test_invalid_encryption_key_is_rejected(self):
        environment = self.environment()
        environment["QR_ENCRYPTION_KEYS"] = "invalid-key"
        with self.assertRaises(ConfigurationError):
            Settings.from_env(environment)

    def test_encryption_key_rotation_preserves_key_order(self):
        environment = self.environment()
        new_key = base64.urlsafe_b64encode(b"n" * 32).decode()
        old_key = environment["QR_ENCRYPTION_KEYS"]
        environment["QR_ENCRYPTION_KEYS"] = f"{new_key},{old_key}"
        self.assertEqual(Settings.from_env(environment).qr_encryption_keys, (new_key, old_key))

    def test_database_identifier_cannot_contain_sql(self):
        environment = self.environment()
        environment["DB_NAME"] = "meals`; DROP DATABASE example"
        with self.assertRaises(ConfigurationError):
            Settings.from_env(environment)


if __name__ == "__main__":
    unittest.main()
