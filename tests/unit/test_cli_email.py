import base64
import contextlib
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from meal_management.cli import main
from meal_management.config import Settings


class CliEmailTests(unittest.TestCase):
    def invoke(self, *, arguments=None, backend="gmail", enabled=True, status="SENT", error=None, production=False, tls=True):
        runtime = SimpleNamespace(
            email_backend=backend,
            email_send_enabled=enabled,
            environment="production" if production else "development",
        )
        settings = Settings(
            db_host="127.0.0.1", db_port=3306, db_name="fictional_mail_test",
            db_user="fictional_mail_user", db_password="private-database-password",
            qr_encryption_keys=(base64.urlsafe_b64encode(b"x" * 32).decode(),),
            db_ssl_ca="fictional-ca.pem" if tls else None,
        )
        output, errors = io.StringIO(), io.StringIO()
        worker = Mock()
        worker.send.return_value = {"email_id": 7, "status": status}
        worker.send.side_effect = error
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch("meal_management.cli._load_environment"))
            load_runtime = stack.enter_context(patch("meal_management.cli.RuntimeSettings.from_env", return_value=runtime))
            load_settings = stack.enter_context(patch("meal_management.cli.Settings.from_env", return_value=settings))
            database = stack.enter_context(patch("meal_management.cli.Database"))
            delivery = stack.enter_context(patch("meal_management.cli.delivery_from_settings"))
            constructor = stack.enter_context(patch("meal_management.cli.EmailDeliveryWorker", return_value=worker))
            stack.enter_context(contextlib.redirect_stdout(output))
            stack.enter_context(contextlib.redirect_stderr(errors))
            code = main(arguments or ["send-email", "--email-id", "7", "--allow-database-changes", "--allow-real-email"])
        return SimpleNamespace(
            code=code, stdout=output.getvalue(), stderr=errors.getvalue(), worker=worker,
            load_runtime=load_runtime, load_settings=load_settings, database=database,
            delivery=delivery, constructor=constructor,
        )

    def test_each_approval_is_required_before_reading_credentials_or_constructing_services(self):
        for flags, error in (
            ([], "EXPLICIT_DATABASE_CHANGE_APPROVAL_REQUIRED"),
            (["--allow-real-email"], "EXPLICIT_DATABASE_CHANGE_APPROVAL_REQUIRED"),
            (["--allow-database-changes"], "EXPLICIT_REAL_EMAIL_APPROVAL_REQUIRED"),
        ):
            with self.subTest(flags=flags):
                result = self.invoke(arguments=["send-email", "--email-id", "7", *flags])
                self.assertEqual(result.code, 1)
                self.assertIn(error, result.stderr)
                result.load_runtime.assert_not_called()
                result.load_settings.assert_not_called()
                result.database.assert_not_called()
                result.delivery.assert_not_called()
                result.worker.send.assert_not_called()

    def test_email_id_must_be_positive_and_fit_database(self):
        for identifier in ("0", "-1", "18446744073709551616"):
            with self.subTest(identifier=identifier):
                result = self.invoke(arguments=["send-email", "--email-id", identifier, "--allow-database-changes", "--allow-real-email"])
                self.assertEqual(result.code, 1)
                self.assertIn("INVALID_EMAIL_ID", result.stderr)
                result.load_runtime.assert_not_called()
                result.database.assert_not_called()

    def test_preview_and_disabled_modes_cannot_connect_or_send(self):
        for backend, enabled, code in (
            ("preview", False, "REAL_EMAIL_BACKEND_REQUIRED"),
            ("preview", True, "REAL_EMAIL_BACKEND_REQUIRED"),
            ("gmail", False, "REAL_EMAIL_SENDING_DISABLED"),
            ("ses", False, "REAL_EMAIL_SENDING_DISABLED"),
        ):
            with self.subTest(backend=backend, enabled=enabled):
                result = self.invoke(backend=backend, enabled=enabled)
                self.assertEqual(result.code, 1)
                self.assertIn(code, result.stderr)
                result.load_settings.assert_not_called()
                result.database.assert_not_called()
                result.delivery.assert_not_called()

    def test_only_the_selected_email_is_sent_with_explicit_permission(self):
        for backend in ("gmail", "ses"):
            with self.subTest(backend=backend):
                result = self.invoke(backend=backend)
                self.assertEqual(result.code, 0)
                self.assertEqual(json.loads(result.stdout), {"email_id": 7, "status": "SENT"})
                self.assertEqual(result.stderr, "")
                result.worker.send.assert_called_once_with(7, allow_real_email=True)
                result.database.assert_called_once()
                result.delivery.assert_called_once()

    def test_already_sent_replay_is_success_and_other_outcomes_are_nonzero(self):
        for status, expected in (("ALREADY_SENT", 0), ("CANCELLED", 2), ("FAILED", 2), ("NEEDS_REVIEW", 2)):
            with self.subTest(status=status):
                result = self.invoke(status=status)
                self.assertEqual(result.code, expected)
                self.assertEqual(json.loads(result.stdout)["status"], status)

    def test_failure_does_not_print_provider_or_database_details(self):
        secret = "sensitive-smtp-password-and-qr"
        result = self.invoke(error=RuntimeError(secret))
        self.assertEqual(result.code, 1)
        self.assertEqual(result.stdout, "")
        self.assertNotIn(secret, result.stderr)

    def test_production_still_requires_verified_database_tls(self):
        result = self.invoke(production=True, tls=False)
        self.assertEqual(result.code, 1)
        self.assertIn("MISSING_SETTING_DB_SSL_CA", result.stderr)
        result.database.assert_not_called()
        result.delivery.assert_not_called()


if __name__ == "__main__":
    unittest.main()
