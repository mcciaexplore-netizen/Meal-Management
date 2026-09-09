import base64
import contextlib
import io
import json
import secrets
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mysql.connector import Error as MySQLError

from meal_management.cli import main
from meal_management.config import Settings


class SensitiveDatabaseFailure(MySQLError):
    def __init__(self, errno, secret):
        self.msg_reads = 0
        super().__init__(msg=secret, errno=1045)
        self.errno = errno
        self.secret = secret
        self.host = "private-host-" + secret
        self.user = "private-user-" + secret
        self.password = secret
        self.sql = "SELECT confidential FROM private_table WHERE password = '" + secret + "'"
        self.str_calls = 0
        self.repr_calls = 0
        self.msg_reads = 0

    def __str__(self):
        self.str_calls += 1
        return self.secret + " " + self.host + " " + self.user + " " + self.sql

    def __repr__(self):
        self.repr_calls += 1
        return "SensitiveDatabaseFailure(" + self.secret + ")"

    @property
    def msg(self):
        self.msg_reads += 1
        return self._sensitive_message

    @msg.setter
    def msg(self, value):
        self._sensitive_message = value


class IntegerSubclass(int):
    pass


class CliDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.secret = secrets.token_urlsafe(32)
        self.settings = Settings(
            db_host="127.0.0.1",
            db_port=3306,
            db_name="fictional_meal_test",
            db_user="fictional_application",
            db_password=self.secret,
            qr_encryption_keys=(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),),
        )

    def invoke(self, argv, error=None, failure_stage="operation"):
        output = io.StringIO()
        errors = io.StringIO()
        runner = Mock()
        runner.apply.return_value = ()
        runner.baseline.return_value = 1
        runner.inspect.return_value = {"fingerprint": "a" * 64, "tables": ["employees"], "migrations": []}
        if error is not None and failure_stage == "operation":
            runner.apply.side_effect = error
            runner.baseline.side_effect = error
            runner.inspect.side_effect = error
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch("meal_management.cli._load_environment"))
            settings_loader = stack.enter_context(patch("meal_management.cli.Settings.from_env", return_value=self.settings))
            database = stack.enter_context(patch("meal_management.cli.Database", side_effect=error if failure_stage == "database" else None))
            migration_runner = stack.enter_context(patch("meal_management.cli.MigrationRunner", return_value=runner, side_effect=error if failure_stage == "runner" else None))
            stack.enter_context(contextlib.redirect_stdout(output))
            stack.enter_context(contextlib.redirect_stderr(errors))
            result = main(argv)
        return SimpleNamespace(
            result=result, stdout=output.getvalue(), stderr=errors.getvalue(),
            settings_loader=settings_loader, database=database,
            migration_runner=migration_runner, runner=runner,
        )

    def assert_failure_is_private(self, result, error):
        self.assertEqual(result.result, 1)
        self.assertEqual(result.stdout, "")
        self.assertNotIn(self.secret, result.stderr)
        self.assertNotIn(error.host, result.stderr)
        self.assertNotIn(error.user, result.stderr)
        self.assertNotIn(error.sql, result.stderr)
        self.assertNotIn("SELECT", result.stderr)
        self.assertEqual(error.str_calls, 0)
        self.assertEqual(error.repr_calls, 0)
        self.assertEqual(error.msg_reads, 0)

    def test_common_mysql_errors_have_fixed_safe_guidance(self):
        cases = (
            (1045, "authentication"),
            (1044, "permission"),
            (1049, "does not exist"),
            (2002, "connect"),
            (2003, "connect"),
            (2026, "tls"),
        )
        for errno, expected in cases:
            with self.subTest(errno=errno):
                error = SensitiveDatabaseFailure(errno, self.secret)
                result = self.invoke(["migrate", "--allow-database-changes"], error)
                self.assertIn("MySQL error " + str(errno), result.stderr)
                self.assertIn(expected, result.stderr.lower())
                self.assert_failure_is_private(result, error)

    def test_other_valid_mysql_error_keeps_numeric_code_and_safe_generic_message(self):
        for errno in (1, 3823, 65535):
            with self.subTest(errno=errno):
                error = SensitiveDatabaseFailure(errno, self.secret)
                result = self.invoke(["migrate", "--allow-database-changes"], error)
                self.assertIn("MySQL error " + str(errno), result.stderr)
                self.assertIn("operation failed", result.stderr.lower())
                self.assert_failure_is_private(result, error)

    def test_invalid_errno_values_use_original_generic_message(self):
        values = ("1045 " + self.secret, "1045", True, False, None, -1, 0, 65536, IntegerSubclass(1045))
        for errno in values:
            with self.subTest(errno_type=type(errno).__name__):
                error = SensitiveDatabaseFailure(errno, self.secret)
                result = self.invoke(["migrate", "--allow-database-changes"], error)
                self.assertEqual(result.stderr, "Operation failed. Check configuration and database availability.\n")
                self.assert_failure_is_private(result, error)

    def test_exception_without_errno_uses_generic_message_without_details(self):
        result = self.invoke(["migrate", "--allow-database-changes"], RuntimeError("private password " + self.secret))
        self.assertEqual(result.result, 1)
        self.assertEqual(result.stderr, "Operation failed. Check configuration and database availability.\n")
        self.assertNotIn(self.secret, result.stderr)

    def test_operating_system_errors_are_not_mislabeled_as_mysql_errors(self):
        for errno in (2, 1045):
            with self.subTest(errno=errno):
                result = self.invoke(["migrate", "--allow-database-changes"], OSError(errno, self.secret))
                self.assertEqual(result.result, 1)
                self.assertEqual(result.stderr, "Operation failed. Check configuration and database availability.\n")
                self.assertNotIn(self.secret, result.stderr)

    def test_database_and_runner_construction_failures_are_safely_diagnosed(self):
        for stage in ("database", "runner"):
            with self.subTest(stage=stage):
                error = SensitiveDatabaseFailure(1045, self.secret)
                result = self.invoke(["migrate", "--allow-database-changes"], error, failure_stage=stage)
                self.assertIn("MySQL error 1045", result.stderr)
                self.assert_failure_is_private(result, error)
                result.runner.apply.assert_not_called()

    def test_inspect_and_baseline_errors_are_safely_diagnosed(self):
        commands = (
            ["schema-inspect", "--allow-database-access"],
            ["baseline", "--version", "1", "--expected-schema-fingerprint", "a" * 64, "--allow-database-changes"],
        )
        for command in commands:
            with self.subTest(command=command[0]):
                error = SensitiveDatabaseFailure(1044, self.secret)
                result = self.invoke(command, error)
                self.assertIn("MySQL error 1044", result.stderr)
                self.assert_failure_is_private(result, error)

    def test_schema_inspect_dispatch_calls_only_inspect(self):
        result = self.invoke(["schema-inspect", "--allow-database-access"])
        self.assertEqual(result.result, 0)
        self.assertEqual(result.stderr, "")
        self.assertEqual(json.loads(result.stdout), {"fingerprint": "a" * 64, "tables": ["employees"], "migrations": []})
        result.runner.inspect.assert_called_once_with()
        result.runner.apply.assert_not_called()
        result.runner.baseline.assert_not_called()
        result.database.assert_called_once_with(self.settings)

    def test_schema_inspect_requires_explicit_connection_approval(self):
        result = self.invoke(["schema-inspect"])
        self.assertEqual(result.result, 1)
        self.assertIn("EXPLICIT_DATABASE_ACCESS_APPROVAL_REQUIRED", result.stderr)
        result.settings_loader.assert_not_called()
        result.database.assert_not_called()
        result.migration_runner.assert_not_called()
        result.runner.inspect.assert_not_called()
        result.runner.apply.assert_not_called()
        result.runner.baseline.assert_not_called()


if __name__ == "__main__":
    unittest.main()
