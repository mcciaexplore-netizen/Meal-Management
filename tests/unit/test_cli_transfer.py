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

from meal_management.backup import _backup_failure, backup_failure_message
from meal_management.cli import PROJECT_ROOT, main
from meal_management.errors import DomainError


class CliTransferTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.env_file = self.directory / "fictional.env"
        self.backup_path = self.directory / "verified-backup"
        self.password = "fictional-db-password-kept-private"
        self.key = base64.urlsafe_b64encode(b"k" * 32).decode()
        self.values = {
            "APP_ENV": "development", "APP_ORIGIN": "http://localhost:8000",
            "SCANNER_ORIGIN": "http://localhost:8001", "PHOTO_BACKEND": "local",
            "PRIVATE_PHOTO_ROOT": "var/private/photos", "EMAIL_BACKEND": "preview",
            "EMAIL_SEND_ENABLED": "false", "EMAIL_AUTO_SEND_ENABLED": "false",
            "APP_CSRF_SECRET": "a" * 32, "LOGIN_RATE_SECRET": "b" * 32,
            "QR_ENCRYPTION_KEYS": self.key, "DB_HOST": "127.0.0.1", "DB_PORT": "3307",
            "DB_NAME": "fictional_meals", "DB_USER": "fictional_runtime", "DB_PASSWORD": self.password,
        }
        self.write_environment()

    def write_environment(self, changes=None, remove=()):
        values = {**self.values, **(changes or {})}
        for name in remove:
            values.pop(name, None)
        self.env_file.write_text("".join(name + "=" + value + "\n" for name, value in values.items()), encoding="utf-8")

    def arguments(self, command, *, allow=True, paused=True, env=True):
        arguments = ["--env-file", str(self.env_file)] if env else []
        arguments.append(command)
        if command == "restore-aiven":
            arguments.extend(["--backup", str(self.backup_path), "--expected-target", "fictional.aivencloud.com:12345/defaultdb"])
        if allow:
            arguments.append("--allow-database-access" if command == "backup-local" else "--allow-database-changes")
        if paused:
            arguments.append("--writers-paused")
        return arguments

    def invoke(self, arguments, failure=None):
        output = io.StringIO()
        errors = io.StringIO()
        backup = Mock(return_value=self.backup_path, side_effect=failure)
        restore = Mock(return_value={
            "status": "verified", "target": "fictional.aivencloud.com:12345/defaultdb",
            "applied_migrations": [6], "qr_credentials_verified": 5,
        }, side_effect=failure)
        with contextlib.ExitStack() as stack:
            loader = stack.enter_context(patch("meal_management.cli._load_environment"))
            database = stack.enter_context(patch("meal_management.cli.Database"))
            prompt = stack.enter_context(patch("meal_management.cli.getpass.getpass"))
            stack.enter_context(patch.dict(sys.modules, {
                "meal_management.backup": SimpleNamespace(interactive_backup=backup, backup_failure_message=backup_failure_message),
                "meal_management.transfer": SimpleNamespace(restore_aiven_backup=restore),
            }))
            stack.enter_context(contextlib.redirect_stdout(output))
            stack.enter_context(contextlib.redirect_stderr(errors))
            result = main(arguments)
        return SimpleNamespace(
            result=result, stdout=output.getvalue(), stderr=errors.getvalue(),
            backup=backup, restore=restore, loader=loader, database=database, prompt=prompt,
        )

    def assert_no_connections_or_prompts(self, result):
        result.backup.assert_not_called()
        result.restore.assert_not_called()
        result.database.assert_not_called()
        result.prompt.assert_not_called()
        result.loader.assert_not_called()

    def test_backup_reports_safe_login_failure_without_secret_or_success(self):
        failure = _backup_failure(MySQLError(msg=self.password + " private SQL", errno=1045), "SOURCE_SNAPSHOT")
        result = self.invoke(self.arguments("backup-local"), failure)
        self.assertEqual(result.result, 1)
        self.assertIn("SOURCE_SNAPSHOT", result.stderr)
        self.assertIn("MySQL error 1045", result.stderr)
        self.assertIn("MySQL rejected the login", result.stderr)
        self.assertNotIn(self.password, result.stderr + result.stdout)
        self.assertNotIn("private SQL", result.stderr + result.stdout)
        self.assertEqual(result.stdout, "")
        result.database.assert_not_called()
        result.prompt.assert_not_called()
        result.loader.assert_not_called()

    def test_explicit_access_or_change_flag_is_required_before_file_reads(self):
        for command in ("backup-local", "restore-aiven"):
            with self.subTest(command=command), patch("meal_management.cli._transfer_environment") as environment:
                result = self.invoke(self.arguments(command, allow=False))
                self.assertEqual(result.result, 1)
                self.assertIn("EXPLICIT_DATABASE_", result.stderr)
                environment.assert_not_called()
                self.assert_no_connections_or_prompts(result)

    def test_writers_must_be_confirmed_paused_before_file_reads(self):
        for command in ("backup-local", "restore-aiven"):
            with self.subTest(command=command), patch("meal_management.cli._transfer_environment") as environment:
                result = self.invoke(self.arguments(command, paused=False))
                self.assertEqual(result.result, 1)
                self.assertIn("TRANSFER_REQUIRES_PAUSED_WRITERS", result.stderr)
                environment.assert_not_called()
                self.assert_no_connections_or_prompts(result)

    def test_both_commands_require_explicit_environment_file(self):
        for command in ("backup-local", "restore-aiven"):
            result = self.invoke(self.arguments(command, env=False))
            self.assertEqual(result.result, 1)
            self.assertIn("EXPLICIT_ENV_FILE_REQUIRED", result.stderr)
            self.assert_no_connections_or_prompts(result)

    def test_missing_environment_file_fails_without_helpers(self):
        self.env_file.unlink()
        result = self.invoke(self.arguments("backup-local"))
        self.assertEqual(result.result, 1)
        self.assertIn("ENVIRONMENT_FILE_NOT_FOUND", result.stderr)
        self.assert_no_connections_or_prompts(result)

    def test_production_or_nonlocal_photos_are_refused_before_transfer(self):
        for changes, expected in (
            ({"APP_ENV": "production"}, "TRANSFER_REQUIRES_DEVELOPMENT"),
            ({"PHOTO_BACKEND": "s3"}, "TRANSFER_REQUIRES_LOCAL_PHOTOS"),
        ):
            self.write_environment(changes)
            for command in ("backup-local", "restore-aiven"):
                with self.subTest(command=command, changes=changes):
                    result = self.invoke(self.arguments(command))
                    self.assertEqual(result.result, 1)
                    self.assertIn(expected, result.stderr)
                    self.assert_no_connections_or_prompts(result)

    def test_development_and_local_storage_must_be_explicit_in_selected_file(self):
        for name, expected in (("APP_ENV", "TRANSFER_REQUIRES_DEVELOPMENT"), ("PHOTO_BACKEND", "TRANSFER_REQUIRES_LOCAL_PHOTOS")):
            self.write_environment(remove=(name,))
            with patch.dict(os.environ, {name: self.values[name]}):
                result = self.invoke(self.arguments("backup-local"))
            self.assertIn(expected, result.stderr)
            self.assert_no_connections_or_prompts(result)

    def test_backup_uses_only_selected_file_and_delegates_interactive_root_prompt(self):
        original_file = self.env_file.read_bytes()
        with patch.dict(os.environ, {"DB_HOST": "wrong-host", "DB_USER": "wrong-user", "DB_PASSWORD": "wrong-password", "EMAIL_SEND_ENABLED": "true"}):
            before = dict(os.environ)
            result = self.invoke(self.arguments("backup-local"))
            self.assertEqual(dict(os.environ), before)
        self.assertEqual(result.result, 0, result.stderr)
        settings = result.backup.call_args.args[0]
        options = result.backup.call_args.kwargs
        self.assertEqual(settings.db_host, "127.0.0.1")
        self.assertEqual(settings.db_port, 3307)
        self.assertEqual(settings.db_name, "fictional_meals")
        self.assertEqual(settings.db_user, "fictional_runtime")
        self.assertEqual(settings.db_password, self.password)
        self.assertEqual(options, {
            "project_root": PROJECT_ROOT, "env_file": self.env_file.absolute(),
            "photo_root": PROJECT_ROOT / "var/private/photos",
            "migration_directory": PROJECT_ROOT / "database/migrations",
            "allow_database_access": True, "writers_paused": True,
        })
        self.assertEqual(self.env_file.read_bytes(), original_file)
        self.assertNotIn(self.password, result.stdout + result.stderr)
        self.assertNotIn(self.key, result.stdout + result.stderr)
        result.loader.assert_not_called()
        result.restore.assert_not_called()
        result.database.assert_not_called()
        result.prompt.assert_not_called()

    def test_dotenv_values_are_never_interpolated_from_exported_secrets(self):
        literal = "${UNRELATED_PASSWORD}-literal-value"
        self.write_environment({"DB_PASSWORD": literal})
        with patch.dict(os.environ, {"UNRELATED_PASSWORD": "private-exported-secret"}):
            result = self.invoke(self.arguments("backup-local"))
        self.assertEqual(result.result, 0, result.stderr)
        self.assertEqual(result.backup.call_args.args[0].db_password, literal)
        self.assertNotIn("private-exported-secret", result.stdout + result.stderr)

    def test_missing_file_database_password_cannot_fall_back_to_environment(self):
        self.write_environment(remove=("DB_PASSWORD",))
        with patch.dict(os.environ, {"DB_PASSWORD": self.password}):
            result = self.invoke(self.arguments("backup-local"))
        self.assertEqual(result.result, 1)
        self.assertIn("MISSING_CONFIGURATION: DB_PASSWORD", result.stderr)
        self.assert_no_connections_or_prompts(result)

    def test_restore_passes_exact_target_and_safe_local_runtime_without_switching_configuration(self):
        self.write_environment({
            "DB_HOST": "fictional.aivencloud.com", "DB_PORT": "12345", "DB_NAME": "defaultdb",
            "DB_USER": "fictional_migrator", "DB_SSL_CA": str(self.directory / "aiven-ca.pem"),
        })
        original_file = self.env_file.read_bytes()
        result = self.invoke(self.arguments("restore-aiven"))
        self.assertEqual(result.result, 0, result.stderr)
        settings, runtime, backup_path, migrations = result.restore.call_args.args
        self.assertEqual((settings.db_host, settings.db_port, settings.db_name), ("fictional.aivencloud.com", 12345, "defaultdb"))
        self.assertEqual(settings.db_ssl_ca, str(self.directory / "aiven-ca.pem"))
        self.assertEqual(runtime.environment, "development")
        self.assertEqual(runtime.photo_backend, "local")
        self.assertFalse(runtime.email_send_enabled)
        self.assertFalse(runtime.email_auto_send_enabled)
        self.assertEqual(backup_path, self.backup_path.absolute())
        self.assertEqual(migrations, PROJECT_ROOT / "database/migrations")
        self.assertEqual(result.restore.call_args.kwargs, {
            "expected_target": "fictional.aivencloud.com:12345/defaultdb",
            "allow_database_changes": True, "writers_paused": True,
        })
        self.assertEqual(self.env_file.read_bytes(), original_file)
        result.backup.assert_not_called()
        result.loader.assert_not_called()
        result.database.assert_not_called()

    def test_custom_migration_directory_is_resolved_for_both_helpers(self):
        directory = self.directory / "migration-fixture"
        for command in ("backup-local", "restore-aiven"):
            result = self.invoke([*self.arguments(command), "--directory", str(directory)])
            self.assertEqual(result.result, 0)
            actual = result.backup.call_args.kwargs["migration_directory"] if command == "backup-local" else result.restore.call_args.args[3]
            self.assertEqual(actual, directory.resolve())

    def test_selected_paths_preserve_symlinks_for_helper_safety_checks(self):
        linked_env = self.directory / "linked.env"
        linked_env.symlink_to(self.env_file)
        arguments = self.arguments("backup-local")
        arguments[1] = str(linked_env)
        result = self.invoke(arguments)
        self.assertEqual(result.backup.call_args.kwargs["env_file"], linked_env.absolute())
        linked_backup = self.directory / "linked-backup"
        linked_backup.symlink_to(self.backup_path)
        arguments = self.arguments("restore-aiven")
        arguments[arguments.index("--backup") + 1] = str(linked_backup)
        result = self.invoke(arguments)
        self.assertEqual(result.restore.call_args.args[2], linked_backup.absolute())

    def test_helper_confirmation_and_secure_prompt_errors_propagate_as_safe_codes(self):
        for code in ("BACKUP_CONFIRMATION_MISMATCH", "SECURE_PASSWORD_PROMPT_UNAVAILABLE", "BACKUP_REQUIRES_INTERACTIVE_TERMINAL"):
            result = self.invoke(self.arguments("backup-local"), DomainError(code))
            self.assertEqual(result.result, 1)
            self.assertEqual(result.stderr, code + "\n")
            self.assertEqual(result.stdout, "")

    def test_failed_backup_or_restore_never_prints_driver_password_or_sql(self):
        for command in ("backup-local", "restore-aiven"):
            for error in (RuntimeError(self.password + " SELECT secret"), MySQLError(msg=self.password + " SELECT secret", errno=1045)):
                with self.subTest(command=command, error_type=type(error)):
                    result = self.invoke(self.arguments(command), error)
                    self.assertEqual(result.result, 1)
                    self.assertEqual(result.stdout, "")
                    self.assertNotIn(self.password, result.stderr)
                    self.assertNotIn("SELECT", result.stderr)
                    self.assertNotIn(self.key, result.stderr)

    def test_failed_restore_does_not_claim_success_or_attempt_backup(self):
        result = self.invoke(self.arguments("restore-aiven"), DomainError("AIVEN_TARGET_NOT_EMPTY"))
        self.assertEqual(result.result, 1)
        self.assertEqual(result.stdout, "")
        result.backup.assert_not_called()
        result.restore.assert_called_once()
