import base64
import contextlib
import getpass
import io
import os
import secrets
import unittest
import warnings
from dataclasses import FrozenInstanceError
from unittest.mock import Mock, patch

from meal_management.cli import main
from meal_management.config import Settings


class CliDatabaseCredentialTests(unittest.TestCase):
    def setUp(self):
        self.application_password = secrets.token_urlsafe(32)
        self.elevated_password = secrets.token_urlsafe(32)
        self.settings = Settings(
            db_host="127.0.0.1",
            db_port=3307,
            db_name="fictional_meal_test",
            db_user="fictional_application",
            db_password=self.application_password,
            qr_encryption_keys=(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),),
            db_connect_timeout=17,
            db_ssl_ca="fictional-ca.pem",
        )
        self.commands = (
            ("migrate", [], "--allow-database-changes"),
            ("schema-inspect", [], "--allow-database-access"),
            ("baseline", ["--version", "1", "--expected-schema-fingerprint", "a" * 64], "--allow-database-changes"),
        )

    def run_cli(self, command, *, allowed=True, override=True, interactive=True, password=None, database_user="fictional_migration", prompt_effect=None, runner_effect=None, database_effect=None):
        name, arguments, approval = command
        argv = [name, *arguments]
        if allowed:
            argv.append(approval)
        if override:
            argv.extend(["--database-user", database_user])
        captured = SimpleCapture()
        captured.runner = Mock()
        captured.runner.apply.return_value = ()
        captured.runner.inspect.return_value = {"fingerprint": "a" * 64, "tables": [], "migrations": []}
        captured.runner.baseline.return_value = 1
        if runner_effect:
            captured.runner.apply.side_effect = runner_effect
            captured.runner.inspect.side_effect = runner_effect
            captured.runner.baseline.side_effect = runner_effect
        with contextlib.ExitStack() as stack:
            captured.environment_loader = stack.enter_context(patch("meal_management.cli._load_environment"))
            captured.settings_loader = stack.enter_context(patch("meal_management.cli.Settings.from_env", return_value=self.settings))
            captured.database = stack.enter_context(patch("meal_management.cli.Database", side_effect=database_effect))
            captured.migration_runner = stack.enter_context(patch("meal_management.cli.MigrationRunner", return_value=captured.runner))
            captured.prompt = stack.enter_context(patch("meal_management.cli.getpass.getpass", return_value=self.elevated_password if password is None else password, side_effect=prompt_effect))
            stack.enter_context(patch("meal_management.cli.sys.stdin.isatty", return_value=interactive))
            stack.enter_context(contextlib.redirect_stdout(captured.stdout))
            stack.enter_context(contextlib.redirect_stderr(captured.stderr))
            captured.result = main(argv)
        return captured

    def assert_safe_output(self, captured):
        output = captured.stdout.getvalue() + captured.stderr.getvalue()
        self.assertNotIn(self.application_password, output)
        self.assertNotIn(self.elevated_password, output)

    def test_all_commands_require_approval_before_credentials_or_database(self):
        for command in self.commands:
            with self.subTest(command=command[0]):
                result = self.run_cli(command, allowed=False)
                self.assertEqual(result.result, 1)
                self.assertIn("EXPLICIT_DATABASE_", result.stderr.getvalue())
                result.prompt.assert_not_called()
                result.settings_loader.assert_not_called()
                result.database.assert_not_called()
                result.migration_runner.assert_not_called()

    def test_optional_user_overrides_only_in_memory_database_credentials(self):
        for command in self.commands:
            with self.subTest(command=command[0]):
                result = self.run_cli(command)
                self.assertEqual(result.result, 0, result.stderr.getvalue())
                result.prompt.assert_called_once()
                result.database.assert_called_once()
                configured = result.database.call_args.args[0]
                self.assertIsInstance(configured, Settings)
                self.assertIsNot(configured, self.settings)
                self.assertEqual(configured.db_user, "fictional_migration")
                self.assertEqual(configured.db_password, self.elevated_password)
                self.assertEqual(configured.db_host, self.settings.db_host)
                self.assertEqual(configured.db_port, self.settings.db_port)
                self.assertEqual(configured.db_name, self.settings.db_name)
                self.assertEqual(configured.db_connect_timeout, self.settings.db_connect_timeout)
                self.assertEqual(configured.db_ssl_ca, self.settings.db_ssl_ca)
                self.assertEqual(configured.qr_encryption_keys, self.settings.qr_encryption_keys)
                self.assertEqual(self.settings.db_user, "fictional_application")
                self.assertEqual(self.settings.db_password, self.application_password)
                with self.assertRaises(FrozenInstanceError):
                    configured.db_password = "cannot mutate"
                self.assert_safe_output(result)

    def test_configured_credentials_need_no_prompt_without_override(self):
        for command in self.commands:
            with self.subTest(command=command[0]):
                result = self.run_cli(command, override=False, interactive=False)
                self.assertEqual(result.result, 0, result.stderr.getvalue())
                result.prompt.assert_not_called()
                self.assertIs(result.database.call_args.args[0], self.settings)
                self.assert_safe_output(result)

    def test_optional_user_rejects_noninteractive_password_before_database(self):
        for command in self.commands:
            with self.subTest(command=command[0]):
                result = self.run_cli(command, interactive=False)
                self.assertEqual(result.result, 1)
                self.assertIn("DATABASE_PASSWORD_REQUIRES_INTERACTIVE_TERMINAL", result.stderr.getvalue())
                result.prompt.assert_not_called()
                result.database.assert_not_called()
                result.migration_runner.assert_not_called()

    def test_blank_password_is_rejected_before_database(self):
        for command in self.commands:
            with self.subTest(command=command[0]):
                result = self.run_cli(command, password="")
                self.assertEqual(result.result, 1)
                self.assertIn("DATABASE_PASSWORD_REQUIRED", result.stderr.getvalue())
                result.database.assert_not_called()
                result.migration_runner.assert_not_called()

    def test_invalid_database_user_is_rejected_before_password_prompt(self):
        for database_user in ("", " ", "name\nline", "x" * 33):
            with self.subTest(database_user=database_user):
                result = self.run_cli(self.commands[0], database_user=database_user)
                self.assertEqual(result.result, 1)
                self.assertIn("INVALID_DATABASE_USER", result.stderr.getvalue())
                result.prompt.assert_not_called()
                result.database.assert_not_called()

    def test_secure_prompt_warning_prevents_echo_fallback(self):
        after_warning = Mock()

        def unsafe_prompt(*args, **kwargs):
            warnings.warn("Password input may be echoed.", getpass.GetPassWarning)
            after_warning()
            return self.elevated_password

        for command in self.commands:
            with self.subTest(command=command[0]):
                result = self.run_cli(command, prompt_effect=unsafe_prompt)
                self.assertEqual(result.result, 1)
                self.assertIn("SECURE_PASSWORD_PROMPT_UNAVAILABLE", result.stderr.getvalue())
                result.database.assert_not_called()
                result.migration_runner.assert_not_called()
                after_warning.assert_not_called()
                self.assert_safe_output(result)

    def test_direct_getpass_warning_exception_is_safe(self):
        result = self.run_cli(self.commands[0], prompt_effect=getpass.GetPassWarning("unsafe echo"))
        self.assertEqual(result.result, 1)
        self.assertIn("SECURE_PASSWORD_PROMPT_UNAVAILABLE", result.stderr.getvalue())
        result.database.assert_not_called()

    def test_override_does_not_change_environment_or_source_settings(self):
        environment = {"DB_USER": "fictional_application", "DB_PASSWORD": self.application_password, "UNRELATED_SETTING": "preserve"}
        with patch.dict(os.environ, environment, clear=True):
            before = dict(os.environ)
            result = self.run_cli(self.commands[0])
            self.assertEqual(result.result, 0)
            self.assertEqual(dict(os.environ), before)
        self.assertEqual(self.settings.db_password, self.application_password)
        result.environment_loader.assert_called_once_with(None)
        self.assert_safe_output(result)

    def test_entered_password_is_not_trimmed_or_transformed(self):
        password = "  " + self.elevated_password + "  "
        result = self.run_cli(self.commands[0], password=password)
        self.assertEqual(result.result, 0)
        self.assertEqual(result.database.call_args.args[0].db_password, password)

    def test_database_construction_failure_never_prints_entered_password(self):
        result = self.run_cli(self.commands[0], database_effect=RuntimeError("Connection refused with " + self.elevated_password))
        self.assertEqual(result.result, 1)
        self.assert_safe_output(result)
        result.migration_runner.assert_not_called()

    def test_migration_failures_never_print_entered_password(self):
        for command in self.commands:
            with self.subTest(command=command[0]):
                result = self.run_cli(command, runner_effect=RuntimeError("Driver included password " + self.elevated_password))
                self.assertEqual(result.result, 1)
                self.assert_safe_output(result)


class SimpleCapture:
    def __init__(self):
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()


if __name__ == "__main__":
    unittest.main()
