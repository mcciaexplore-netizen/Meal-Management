import importlib.util
import os
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


def load_support(environment):
    path = Path(__file__).resolve().parents[1] / "integration" / "mysql_support.py"
    specification = importlib.util.spec_from_file_location("meal_test_mysql_support", path)
    module = importlib.util.module_from_spec(specification)
    with patch.dict(os.environ, environment, clear=True):
        specification.loader.exec_module(module)
    return module


class IntegrationSafetyTests(unittest.TestCase):
    def test_mysql_connection_requires_all_opt_in_flags_and_test_name(self):
        for environment in (
            {},
            {"MEAL_RUN_MYSQL_TESTS": "1"},
            {"MEAL_RUN_MYSQL_TESTS": "1", "MEAL_ALLOW_TEST_SCHEMA_CHANGES": "1"},
            {"MEAL_RUN_MYSQL_TESTS": "1", "MEAL_ALLOW_TEST_SCHEMA_CHANGES": "1", "MEAL_TEST_DB_NAME": "production"},
            {"MEAL_RUN_MYSQL_TESTS": "1", "MEAL_ALLOW_TEST_SCHEMA_CHANGES": "1", "MEAL_TEST_DB_NAME": "unsafe`;_test"},
        ):
            with self.subTest(environment=environment):
                self.assertFalse(load_support(environment).MYSQL_TESTS_ENABLED)

    def test_only_valid_test_configuration_enables_integration(self):
        module = load_support({
            "MEAL_RUN_MYSQL_TESTS": "1",
            "MEAL_ALLOW_TEST_SCHEMA_CHANGES": "1",
            "MEAL_TEST_DB_NAME": "meal_management_test",
        })
        self.assertTrue(module.MYSQL_TESTS_ENABLED)

    def test_admin_test_connection_honors_tls_verification_settings(self):
        module = load_support({})
        settings = Mock(
            db_host="localhost", db_port=3306, db_user="test-user",
            db_password="fictional-test-password", db_connect_timeout=10,
            db_ssl_ca="/example/test-ca.pem",
        )
        options = module.admin_connection_options(settings)
        self.assertEqual(options["ssl_ca"], settings.db_ssl_ca)
        self.assertTrue(options["ssl_verify_cert"])
        self.assertTrue(options["ssl_verify_identity"])
        self.assertNotIn("database", options)

    def test_mysql_84_version_is_required_before_schema_creation(self):
        module = load_support({})
        module.require_mysql_84("8.4.9")
        module.require_mysql_84("8.4.9-commercial")
        for version in (None, "8.0.44", "9.0.1", "10.11.8-MariaDB", "8.40.1"):
            with self.subTest(version=version):
                with self.assertRaises(AssertionError):
                    module.require_mysql_84(version)

    def test_trigger_bodies_remain_complete_when_schema_is_split(self):
        module = load_support({})
        source = (
            "SET time_zone = '+00:00';\n"
            "DELIMITER $$\n"
            "CREATE TRIGGER example BEFORE INSERT ON meals FOR EACH ROW\n"
            "BEGIN\n"
            "SET NEW.unit_number = 1;\n"
            "END$$\n"
            "DELIMITER ;\n"
            "SELECT 1;\n"
        )
        statements = list(module.schema_statements(source))
        self.assertEqual(len(statements), 3)
        self.assertTrue(statements[1].startswith("CREATE TRIGGER"))
        self.assertTrue(statements[1].endswith("END"))
        self.assertIn("SET NEW.unit_number = 1;", statements[1])
        self.assertEqual(statements[2], "SELECT 1")

    def test_schema_splitter_rejects_unterminated_statements(self):
        module = load_support({})
        with self.assertRaises(ValueError):
            list(module.schema_statements("CREATE TABLE incomplete (id INT)"))

    def test_project_schema_can_be_split_without_database_access(self):
        module = load_support({})
        path = Path(__file__).resolve().parents[2] / "database" / "schema.sql"
        source = path.read_text(encoding="utf-8")
        statements = list(module.schema_statements(source))
        self.assertGreater(len(statements), 20)
        self.assertTrue(any(statement.startswith("CREATE TABLE meals") for statement in statements))
        self.assertTrue(any(statement.startswith("CREATE TRIGGER") for statement in statements))
        self.assertFalse(any(statement.startswith("DELIMITER") for statement in statements))


if __name__ == "__main__":
    unittest.main()
