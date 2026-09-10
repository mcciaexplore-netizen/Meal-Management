import base64
import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mysql.connector import Error as MySQLError

from meal_management.cli import _runtime_account_failure_message, main
from meal_management.errors import DomainError


class CliRuntimeAccountTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.env_file = self.root / "fictional-aiven.env"
        self.output = self.root / "var/private/vercel/database-runtime.env"
        self.password = "fictional-provider-password-private"
        self.key = base64.urlsafe_b64encode(b"k" * 32).decode()
        self.target = "fictional.aivencloud.com:12345/defaultdb"
        self.confirmation = "CREATE meal_runtime@% ON " + self.target
        self.values = {
            "APP_ENV": "development", "APP_ORIGIN": "http://localhost:8000",
            "SCANNER_ORIGIN": "http://localhost:8001", "PHOTO_BACKEND": "local",
            "PRIVATE_PHOTO_ROOT": "var/private/photos", "EMAIL_BACKEND": "preview",
            "EMAIL_SEND_ENABLED": "false", "EMAIL_AUTO_SEND_ENABLED": "false",
            "APP_CSRF_SECRET": "a" * 32, "LOGIN_RATE_SECRET": "b" * 32,
            "QR_ENCRYPTION_KEYS": self.key, "DB_HOST": "fictional.aivencloud.com", "DB_PORT": "12345",
            "DB_NAME": "defaultdb", "DB_USER": "avnadmin", "DB_PASSWORD": self.password,
            "DB_SSL_CA": str(self.root / "fictional-ca.pem"),
        }
        self.write_environment()

    def write_environment(self, changes=None, remove=()):
        values = {**self.values, **(changes or {})}
        for name in remove:
            values.pop(name, None)
        self.env_file.write_text("".join(name + "=" + value + "\n" for name, value in values.items()))

    def arguments(self, *, allow=True, env=True):
        result = ["--env-file", str(self.env_file)] if env else []
        result.append("create-aiven-runtime")
        if allow:
            result.append("--allow-database-changes")
        return result

    def invoke(self, *, arguments=None, answer=None, tty=True, failure=None, target_failure=None, result=None):
        output = io.StringIO()
        errors = io.StringIO()
        validated = Mock(return_value=self.confirmation, side_effect=target_failure)

        def create(settings, runtime, directory, path, *, confirmation, allow_database_changes):
            if confirmation != self.confirmation:
                raise DomainError("RUNTIME_ACCOUNT_CONFIRMATION_MISMATCH")
            if failure is not None:
                raise failure
            return result if result is not None else {
                "target": self.target, "account": "meal_runtime@%", "credentials_file": str(self.output),
                "status": "verified", "password": "fictional-generated-password-private",
            }

        creator = Mock(side_effect=create)
        module = SimpleNamespace(
            target_confirmation=validated, create_aiven_runtime_account=creator,
            INSERT_TABLES=("employees", "meals", "audit_events"), UPDATE_TABLES=("employees", "email_queue"),
        )
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.dict(sys.modules, {"meal_management.runtime_account": module}))
            stack.enter_context(patch("meal_management.cli.PROJECT_ROOT", self.root))
            stack.enter_context(patch("meal_management.cli.sys.stdin.isatty", return_value=tty))
            loader = stack.enter_context(patch("meal_management.cli._load_environment"))
            database = stack.enter_context(patch("meal_management.cli.Database"))
            password_prompt = stack.enter_context(patch("meal_management.cli.getpass.getpass"))
            prompt = stack.enter_context(patch("builtins.input", return_value=self.confirmation if answer is None else answer))
            stack.enter_context(contextlib.redirect_stdout(output))
            stack.enter_context(contextlib.redirect_stderr(errors))
            code = main(self.arguments() if arguments is None else arguments)
        return SimpleNamespace(
            code=code, stdout=output.getvalue(), stderr=errors.getvalue(), validated=validated,
            creator=creator, prompt=prompt, loader=loader, database=database, password_prompt=password_prompt,
        )

    def assert_no_activity(self, result):
        result.validated.assert_not_called()
        result.creator.assert_not_called()
        result.prompt.assert_not_called()
        result.loader.assert_not_called()
        result.database.assert_not_called()
        result.password_prompt.assert_not_called()

    def test_help_exposes_only_explicit_change_flag_and_does_not_load_configuration(self):
        output = io.StringIO()
        with patch("meal_management.cli._transfer_environment") as load, patch("meal_management.cli.Database") as database:
            with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as error:
                main(["create-aiven-runtime", "--help"])
        self.assertEqual(error.exception.code, 0)
        self.assertIn("--allow-database-changes", output.getvalue())
        self.assertNotIn("--password", output.getvalue())
        self.assertNotIn("--output", output.getvalue())
        load.assert_not_called()
        database.assert_not_called()

    def test_missing_change_flag_fails_before_environment_reads(self):
        with patch("meal_management.cli._transfer_environment") as load:
            result = self.invoke(arguments=self.arguments(allow=False))
        self.assertEqual(result.code, 1)
        self.assertIn("EXPLICIT_DATABASE_CHANGE_APPROVAL_REQUIRED", result.stderr)
        load.assert_not_called()
        self.assert_no_activity(result)

    def test_explicit_environment_and_interactive_terminal_are_required_before_reads(self):
        for arguments, tty, expected in (
            (self.arguments(env=False), True, "EXPLICIT_ENV_FILE_REQUIRED"),
            (self.arguments(), False, "RUNTIME_ACCOUNT_REQUIRES_INTERACTIVE_TERMINAL"),
        ):
            with self.subTest(expected=expected), patch("meal_management.cli._transfer_environment") as load:
                result = self.invoke(arguments=arguments, tty=tty)
            self.assertEqual(result.code, 1)
            self.assertIn(expected, result.stderr)
            load.assert_not_called()
            self.assert_no_activity(result)

    def test_missing_selected_file_fails_before_target_or_account_helpers(self):
        self.env_file.unlink()
        result = self.invoke()
        self.assertIn("ENVIRONMENT_FILE_NOT_FOUND", result.stderr)
        self.assert_no_activity(result)

    def test_target_validation_precedes_printing_grants_or_requesting_confirmation(self):
        result = self.invoke(target_failure=DomainError("RUNTIME_ACCOUNT_INVALID_TARGET"))
        self.assertEqual(result.code, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "RUNTIME_ACCOUNT_INVALID_TARGET\n")
        result.validated.assert_called_once()
        result.creator.assert_not_called()
        result.prompt.assert_not_called()

    def test_wrong_confirmation_is_passed_unchanged_to_refusing_helper(self):
        result = self.invoke(answer="yes")
        self.assertEqual(result.code, 1)
        self.assertEqual(result.stderr, "RUNTIME_ACCOUNT_CONFIRMATION_MISMATCH\n")
        self.assertEqual(result.creator.call_args.kwargs["confirmation"], "yes")
        self.assertNotIn("created and verified", result.stdout)
        self.assertFalse(self.output.exists())
        result.database.assert_not_called()

    def test_success_uses_fixed_paths_and_exact_grant_preview_without_printing_credentials(self):
        original = self.env_file.read_bytes()
        result = self.invoke()
        self.assertEqual(result.code, 0, result.stderr)
        settings, runtime, directory, output = result.creator.call_args.args
        self.assertEqual((settings.db_host, settings.db_port, settings.db_name, settings.db_user), ("fictional.aivencloud.com", 12345, "defaultdb", "avnadmin"))
        self.assertEqual(settings.db_password, self.password)
        self.assertEqual(runtime.environment, "development")
        self.assertEqual(directory, self.root / "database/migrations")
        self.assertEqual(output, self.output)
        self.assertEqual(result.creator.call_args.kwargs, {"confirmation": self.confirmation, "allow_database_changes": True})
        self.assertIn(self.confirmation, result.prompt.call_args.args[0])
        for text in ("mandatory SSL", "any host that can reach", "SELECT on defaultdb.*", "INSERT on these defaultdb tables: employees, meals, audit_events", "UPDATE on these defaultdb tables: employees, email_queue", "meal_runtime@% created and verified", str(self.output), "No deployment or application configuration switch"):
            self.assertIn(text, result.stdout)
        for secret in (self.password, self.key, "fictional-generated-password-private"):
            self.assertNotIn(secret, result.stdout + result.stderr)
        self.assertEqual(self.env_file.read_bytes(), original)
        self.assertFalse(self.output.exists())
        result.loader.assert_not_called()
        result.database.assert_not_called()
        result.password_prompt.assert_not_called()

    def test_only_selected_file_values_are_used_without_mutating_exported_configuration(self):
        environment = {"DB_HOST": "wrong-host", "DB_USER": "wrong-user", "DB_PASSWORD": "wrong-password", "EMAIL_SEND_ENABLED": "true"}
        with patch.dict(os.environ, environment, clear=True):
            result = self.invoke()
            self.assertEqual(dict(os.environ), environment)
        self.assertEqual(result.code, 0)
        settings, runtime = result.creator.call_args.args[:2]
        self.assertEqual(settings.db_host, self.values["DB_HOST"])
        self.assertEqual(settings.db_password, self.password)
        self.assertFalse(runtime.email_send_enabled)
        result.loader.assert_not_called()

    def test_dotenv_interpolation_and_missing_password_fallback_are_disabled(self):
        self.write_environment({"DB_PASSWORD": "${UNRELATED_PASSWORD}-literal"})
        with patch.dict(os.environ, {"UNRELATED_PASSWORD": "private-exported-secret"}, clear=True):
            result = self.invoke()
        self.assertEqual(result.creator.call_args.args[0].db_password, "${UNRELATED_PASSWORD}-literal")
        self.assertNotIn("private-exported-secret", result.stdout + result.stderr)
        self.write_environment(remove=("DB_PASSWORD",))
        with patch.dict(os.environ, {"DB_PASSWORD": "private-exported-secret"}, clear=True):
            result = self.invoke()
        self.assertEqual(result.code, 1)
        self.assertIn("MISSING_CONFIGURATION: DB_PASSWORD", result.stderr)
        self.assert_no_activity(result)

    def test_driver_and_unexpected_failure_messages_never_print_private_details(self):
        for failure in (RuntimeError(self.password + " private SQL"), MySQLError(msg=self.password + " private SQL", errno=1045)):
            with self.subTest(failure=type(failure)):
                result = self.invoke(failure=failure)
                self.assertEqual(result.code, 1)
                self.assertNotIn(self.password, result.stdout + result.stderr)
                self.assertNotIn("private SQL", result.stdout + result.stderr)
                self.assertNotIn("created and verified", result.stdout)

    def test_safe_partial_account_diagnostics_preserve_pending_credentials(self):
        failure = DomainError("RUNTIME_ACCOUNT_FAILED_REQUIRES_REVIEW")
        failure.runtime_account_stage = "GRANTS"
        failure.runtime_account_mysql_error = 1142
        failure.private_details = self.password
        result = self.invoke(failure=failure)
        self.assertEqual(result.code, 1)
        self.assertIn("Account creation stage: GRANTS", result.stderr)
        self.assertIn("MySQL error 1142", result.stderr)
        self.assertIn("database-runtime.pending.env", result.stderr)
        self.assertIn("Do not rerun", result.stderr)
        self.assertNotIn(self.password, result.stderr)

    def test_untrusted_diagnostic_values_are_not_rendered(self):
        failure = DomainError("RUNTIME_ACCOUNT_FAILED_REQUIRES_REVIEW")
        for stage in (None, self.password, ["GRANTS"], 1):
            failure.runtime_account_stage = stage
            self.assertIsNone(_runtime_account_failure_message(failure))
        failure.runtime_account_stage = "PREFLIGHT"
        for number in (None, self.password, True, 0, -1, 65536):
            failure.runtime_account_mysql_error = number
            self.assertEqual(_runtime_account_failure_message(failure), "Account creation stage: PREFLIGHT.")

    def test_unverified_or_mismatched_helper_result_never_claims_success(self):
        for result in (
            {}, {"status": "pending"},
            {"status": "verified", "account": "root@%", "credentials_file": str(self.output)},
            {"status": "verified", "account": "meal_runtime@%", "credentials_file": str(self.root / "other.env")},
        ):
            with self.subTest(result=result):
                invoked = self.invoke(result=result)
                self.assertEqual(invoked.code, 1)
                self.assertIn("RUNTIME_ACCOUNT_VERIFICATION_UNCONFIRMED", invoked.stderr)
                self.assertNotIn("created and verified", invoked.stdout)


if __name__ == "__main__":
    unittest.main()
