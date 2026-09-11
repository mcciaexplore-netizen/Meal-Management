import base64
import hashlib
import importlib.util
import io
import json
import os
import ssl
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from meal_management.errors import DomainError


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("vercel_prepare_environment", PROJECT_ROOT / "deploy" / "vercel" / "prepare_environment.py")
preparation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preparation)
SENSITIVE_KEYS = {"DB_PASSWORD", "QR_ENCRYPTION_KEYS", "APP_CSRF_SECRET", "LOGIN_RATE_SECRET", "GMAIL_APP_PASSWORD", "SCANNER_ACTIVATION_SECRET"}
EXPECTED_KEYS = {
    "APP_ENV", "APP_ORIGIN", "SCANNER_ORIGIN", "ALLOWED_HOSTS", "COOKIE_SECURE", "APP_CSRF_SECRET",
    "LOGIN_RATE_SECRET", "LOGIN_WINDOW_SECONDS", "DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD",
    "DB_CONNECT_TIMEOUT", "DB_SSL_CA_PEM", "QR_ENCRYPTION_KEYS", "PHOTO_BACKEND", "BLOB_STORE_HOST",
    "MAX_PHOTO_BYTES", "EMAIL_BACKEND", "EMAIL_SENDER", "GMAIL_APP_PASSWORD", "EMAIL_SEND_ENABLED",
    "EMAIL_AUTO_SEND_ENABLED", "EMAIL_PROCESS_LIMIT", "SCAN_APP_ENABLED", "SCANNER_ACTIVATION_SECRET", "SCANNER_ALLOWED_CIDRS",
    "SCAN_REQUEST_LIMIT", "SCAN_IP_LIMIT", "SCAN_WINDOW_SECONDS", "MEAL_APPLICATION",
}


class VercelEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.root.chmod(0o700)
        self.local_path = self.root / ".env"
        self.aiven_path = self.root / ".env.aiven"
        self.runtime_path = self.root / "database-runtime.env"
        self.ca_path = self.root / "aiven-ca.pem"
        self.output = self.root / "prepared"
        self.certificate = "-----BEGIN CERTIFICATE-----\nfictional-certificate\n-----END CERTIFICATE-----\n"
        self.ca_path.write_text(self.certificate, encoding="ascii")
        self.ca_path.chmod(0o644)
        self.local = {
            "APP_ENV": "development", "APP_CSRF_SECRET": "c" * 32, "LOGIN_RATE_SECRET": "l" * 32,
            "DB_HOST": "localhost", "DB_PORT": "3306", "DB_NAME": "meal_management",
            "DB_USER": "local_application", "DB_PASSWORD": "fictional-local-password",
            "QR_ENCRYPTION_KEYS": base64.urlsafe_b64encode(b"k" * 32).decode(),
            "PHOTO_BACKEND": "local", "EMAIL_BACKEND": "gmail", "EMAIL_SENDER": "Fictional.Meals@gmail.com",
            "GMAIL_APP_PASSWORD": "abcd efgh ijkl mnop", "EMAIL_SEND_ENABLED": "true",
            "EMAIL_AUTO_SEND_ENABLED": "true", "BLOB_READ_WRITE_TOKEN": "fictional-local-blob-secret",
            "BLOB_STORE_ID": "fictional-local-store", "BLOB_STORE_API_WEBHOOK_SECRET": "fictional-webhook-secret",
            "BLOB_WEBHOOK_PUBLIC_KEY": "fictional-webhook-public-key",
        }
        self.aiven = dict(self.local, DB_HOST="fictional-mysql.aivencloud.com", DB_PORT="12345", DB_NAME="defaultdb",
                          DB_USER="avnadmin", DB_PASSWORD="fictional-owner-password", DB_SSL_CA=str(self.ca_path),
                          EMAIL_BACKEND="preview", EMAIL_SEND_ENABLED="false", EMAIL_AUTO_SEND_ENABLED="false")
        self.runtime = {
            "DB_HOST": self.aiven["DB_HOST"], "DB_PORT": self.aiven["DB_PORT"], "DB_NAME": "defaultdb",
            "DB_USER": "meal_runtime", "DB_PASSWORD": "fictional-runtime-password",
            "DB_CONNECT_TIMEOUT": "10", "DB_SSL_CA": str(self.ca_path),
        }
        self.save_sources()
        self.ssl_context = patch.object(preparation.ssl, "create_default_context").start()
        self.addCleanup(patch.stopall)
        self.database = patch("meal_management.database.Database._connect", side_effect=AssertionError("No database access permitted")).start()
        self.smtp = patch("smtplib.SMTP_SSL", side_effect=AssertionError("No email access permitted")).start()

    def write_env(self, path, values):
        content = "".join(key + "='" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'\n" for key, value in values.items())
        path.write_text(content, encoding="utf-8")
        path.chmod(0o600)

    def save_sources(self):
        for path, values in ((self.local_path, self.local), (self.aiven_path, self.aiven), (self.runtime_path, self.runtime)):
            self.write_env(path, values)

    def prepare(self, **changes):
        arguments = {
            "local_env": self.local_path, "aiven_env": self.aiven_path, "runtime_env": self.runtime_path,
            "output_directory": self.output, "blob_host": "fictionalstore.private.blob.vercel-storage.com",
            "admin_origin": "https://mccia-meal-admin.vercel.app", "scanner_origin": "https://mccia-meal-scanner.vercel.app",
            "admin_project_id": "prj_fictionalAdmin12345", "scanner_project_id": "prj_fictionalScanner12345",
        }
        arguments.update(changes)
        return preparation.prepare_environment(**arguments)

    def payload(self, application):
        return json.loads((self.output / (application + ".env.json")).read_text())

    def assert_invalid(self, code, **changes):
        with self.assertRaisesRegex(DomainError, code):
            self.prepare(**changes)
        self.assertFalse(self.output.exists())
        self.database.assert_not_called()
        self.smtp.assert_not_called()

    def test_prepares_only_private_allowlisted_production_payloads(self):
        review = self.prepare()
        self.assertEqual({path.name for path in self.output.iterdir()}, {"admin.env.json", "scanner.env.json", "review.json"})
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o700)
        for path in self.output.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        for application in ("admin", "scanner"):
            rows = self.payload(application)
            self.assertEqual({row["key"] for row in rows}, EXPECTED_KEYS)
            self.assertEqual(len(rows), len(EXPECTED_KEYS))
            for row in rows:
                self.assertEqual(set(row), {"key", "value", "type", "target"})
                self.assertEqual(row["target"], ["production"])
                self.assertEqual(row["type"], "sensitive" if row["key"] in SENSITIVE_KEYS else "encrypted")
            self.assertEqual({row["key"] for row in rows if row["type"] == "sensitive"}, SENSITIVE_KEYS)
        self.assertEqual(review["status"], "prepared_offline")
        self.assertFalse(review["deployment_performed"])
        self.database.assert_not_called()
        self.smtp.assert_not_called()

    def test_both_projects_share_admin_origin_and_values_except_application_role(self):
        self.prepare()
        admin = {row["key"]: row["value"] for row in self.payload("admin")}
        scanner = {row["key"]: row["value"] for row in self.payload("scanner")}
        self.assertEqual(admin.pop("MEAL_APPLICATION"), "admin")
        self.assertEqual(scanner.pop("MEAL_APPLICATION"), "scanner")
        self.assertEqual(admin, scanner)
        self.assertEqual(admin["APP_ORIGIN"], "https://mccia-meal-admin.vercel.app")
        self.assertEqual(admin["SCANNER_ORIGIN"], "https://mccia-meal-scanner.vercel.app")
        self.assertEqual(admin["ALLOWED_HOSTS"], "mccia-meal-admin.vercel.app,mccia-meal-scanner.vercel.app")
        for key in ("EMAIL_SEND_ENABLED", "EMAIL_AUTO_SEND_ENABLED", "SCAN_APP_ENABLED"):
            self.assertEqual(admin[key], "false")
        self.assertEqual(admin["EMAIL_PROCESS_LIMIT"], "1")
        self.assertEqual(admin["MAX_PHOTO_BYTES"], "4000000")
        self.assertEqual(admin["LOGIN_WINDOW_SECONDS"], "60")
        self.assertEqual(admin["COOKIE_SECURE"], "true")
        self.assertEqual(admin["APP_ENV"], "production")

    def test_preserves_aiven_keys_and_runtime_credentials_but_uses_local_gmail(self):
        self.aiven["APP_CSRF_SECRET"] = "base64:" + base64.b64encode(b"c" * 32).decode()
        self.aiven["LOGIN_RATE_SECRET"] = "base64:" + base64.b64encode(b"l" * 32).decode()
        self.aiven["EMAIL_SENDER"] = ""
        self.aiven["GMAIL_APP_PASSWORD"] = ""
        self.save_sources()
        self.prepare()
        values = {row["key"]: row["value"] for row in self.payload("admin")}
        for key in ("APP_CSRF_SECRET", "LOGIN_RATE_SECRET", "QR_ENCRYPTION_KEYS"):
            self.assertEqual(values[key], self.aiven[key])
        self.assertEqual(values["DB_USER"], "meal_runtime")
        self.assertEqual(values["DB_PASSWORD"], self.runtime["DB_PASSWORD"])
        self.assertEqual(values["GMAIL_APP_PASSWORD"], "abcdefghijklmnop")
        self.assertEqual(values["EMAIL_SENDER"], "fictional.meals@gmail.com")
        self.assertEqual(values["DB_SSL_CA_PEM"], self.certificate)
        self.assertNotIn("DB_SSL_CA", values)
        self.ssl_context.assert_called_once_with(cadata=self.certificate)

    def test_blob_credentials_and_validation_sentinel_are_never_exported(self):
        review = self.prepare()
        all_content = "\n".join(path.read_text() for path in self.output.iterdir())
        for value in (
            self.local["BLOB_READ_WRITE_TOKEN"], self.local["BLOB_STORE_ID"],
            self.local["BLOB_STORE_API_WEBHOOK_SECRET"], "validation-only-not-a-real-blob-credential",
            self.local["BLOB_WEBHOOK_PUBLIC_KEY"],
        ):
            self.assertNotIn(value, all_content)
        for application in ("admin", "scanner"):
            keys = {row["key"] for row in self.payload(application)}
            self.assertNotIn("BLOB_READ_WRITE_TOKEN", keys)
            self.assertNotIn("BLOB_STORE_ID", keys)
            self.assertNotIn("BLOB_STORE_API_WEBHOOK_SECRET", keys)
            self.assertNotIn("BLOB_WEBHOOK_PUBLIC_KEY", keys)
        self.assertEqual(review["blob_token_source"], "existing_vercel_connection")
        self.assertEqual(review["blob_token_validation"], "not_performed")
        self.assertEqual(review["provider_validation"], "not_performed")

    def test_review_has_project_identity_and_whole_payload_hashes_without_secrets(self):
        review = self.prepare()
        self.assertEqual(review["scope"], "mccias-projects")
        for application in ("admin", "scanner"):
            item = review["projects"][application]
            self.assertEqual(item["name"], "mccia-meal-" + application)
            self.assertEqual(item["payload_file"], application + ".env.json")
            self.assertEqual(item["domains"], ["mccia-meal-" + application + ".vercel.app"])
            self.assertEqual(item["sha256"], hashlib.sha256((self.output / item["payload_file"]).read_bytes()).hexdigest())
        content = (self.output / "review.json").read_text()
        for value in (
            self.local["DB_PASSWORD"], self.aiven["DB_PASSWORD"], self.runtime["DB_PASSWORD"],
            self.aiven["QR_ENCRYPTION_KEYS"], self.aiven["APP_CSRF_SECRET"], self.aiven["LOGIN_RATE_SECRET"],
            "abcdefghijklmnop", "fictional.meals@gmail.com",
        ):
            self.assertNotIn(value, content)

    def test_process_environment_never_supplies_interpolates_or_receives_secrets(self):
        self.runtime["DB_PASSWORD"] = "literal-${INHERITED_RUNTIME_PASSWORD}"
        self.save_sources()
        before = {path: path.read_bytes() for path in (self.local_path, self.aiven_path, self.runtime_path)}
        with patch.dict(os.environ, {"INHERITED_RUNTIME_PASSWORD": "not-allowed-to-inherit", "APP_CSRF_SECRET": "wrong-inherited-secret"}, clear=True):
            snapshot = dict(os.environ)
            self.prepare()
            self.assertEqual(dict(os.environ), snapshot)
        values = {row["key"]: row["value"] for row in self.payload("admin")}
        self.assertEqual(values["DB_PASSWORD"], "literal-${INHERITED_RUNTIME_PASSWORD}")
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)

    def test_missing_source_setting_is_not_filled_from_process_environment(self):
        del self.aiven["APP_CSRF_SECRET"]
        self.save_sources()
        with patch.dict(os.environ, {"APP_CSRF_SECRET": "c" * 32}):
            self.assert_invalid("MISSING_SETTING_APP_CSRF_SECRET")

    def test_mismatched_application_keys_fail_before_creating_output(self):
        for key, replacement in (
            ("APP_CSRF_SECRET", "x" * 32), ("LOGIN_RATE_SECRET", "y" * 32),
            ("QR_ENCRYPTION_KEYS", base64.urlsafe_b64encode(b"z" * 32).decode()),
        ):
            with self.subTest(key=key):
                original = self.aiven[key]
                self.aiven[key] = replacement
                self.save_sources()
                self.assert_invalid("VERCEL_ENV_EXISTING_APPLICATION_KEYS_MUST_MATCH")
                self.aiven[key] = original

    def test_gmail_mismatch_or_wrong_local_backend_fails_before_output(self):
        for key, value in (("EMAIL_SENDER", "different@gmail.com"), ("GMAIL_APP_PASSWORD", "ponmlkjihgfedcba")):
            with self.subTest(key=key):
                original = self.aiven[key]
                self.aiven[key] = value
                self.save_sources()
                self.assert_invalid("VERCEL_ENV_GMAIL_CONFIGURATION_MISMATCH")
                self.aiven[key] = original
        self.local.update(EMAIL_BACKEND="preview", EMAIL_SEND_ENABLED="false", EMAIL_AUTO_SEND_ENABLED="false")
        self.save_sources()
        self.assert_invalid("VERCEL_ENV_LOCAL_GMAIL_CONFIGURATION_REQUIRED")

    def test_restricted_database_target_and_password_must_match_selected_aiven(self):
        variants = (
            ("DB_HOST", "different.aivencloud.com"), ("DB_PORT", "45678"), ("DB_NAME", "otherdb"),
            ("DB_USER", "avnadmin"), ("DB_USER", "root"), ("DB_PASSWORD", self.aiven["DB_PASSWORD"]),
        )
        for key, value in variants:
            with self.subTest(key=key, value=value):
                original = self.runtime[key]
                self.runtime[key] = value
                self.save_sources()
                self.assert_invalid("VERCEL_ENV_RESTRICTED_DATABASE_CONFIGURATION_MISMATCH")
                self.runtime[key] = original
        self.aiven["DB_HOST"] = self.runtime["DB_HOST"] = "localhost"
        self.save_sources()
        self.assert_invalid("VERCEL_ENV_RESTRICTED_DATABASE_CONFIGURATION_MISMATCH")

    def test_ca_paths_and_certificate_validation_are_required(self):
        original = self.runtime["DB_SSL_CA"]
        self.runtime["DB_SSL_CA"] = str(self.root / "different-ca.pem")
        self.save_sources()
        self.assert_invalid("VERCEL_ENV_CA_CONFIGURATION_MISMATCH")
        self.runtime["DB_SSL_CA"] = original
        self.save_sources()
        self.ssl_context.side_effect = ssl.SSLError("fictional certificate invalid")
        self.assert_invalid("VERCEL_ENV_INVALID_CA_CERTIFICATE")

    def test_invalid_or_public_blob_host_is_rejected_before_output(self):
        for host in ("store.public.blob.vercel-storage.com", "https://store.private.blob.vercel-storage.com", "store.private.blob.vercel-storage.com:443"):
            with self.subTest(host=host):
                self.assert_invalid("INVALID_SETTING_BLOB_STORE_HOST", blob_host=host)

    def test_origins_must_be_distinct_named_https_hosts_without_ports_or_paths(self):
        for origin in (
            "http://scanner.example.test", "https://scanner.example.test/path", "https://127.0.0.1",
            "https://localhost", "https://scanner.example.test:8443", "https://mccia-meal-admin.vercel.app",
        ):
            with self.subTest(origin=origin):
                with self.assertRaises(DomainError):
                    self.prepare(scanner_origin=origin)
                self.assertFalse(self.output.exists())

    def test_private_owned_regular_environment_files_are_required(self):
        for path in (self.local_path, self.aiven_path, self.runtime_path):
            with self.subTest(path=path.name):
                path.chmod(0o644)
                self.assert_invalid("VERCEL_ENV_PRIVATE_OWNED_INPUT_REQUIRED")
                path.chmod(0o600)
        original = os.fstat

        def wrong_owner(descriptor):
            values = list(original(descriptor))
            values[4] += 1
            return os.stat_result(values)

        with patch.object(preparation.os, "fstat", side_effect=wrong_owner):
            self.assert_invalid("VERCEL_ENV_PRIVATE_OWNED_INPUT_REQUIRED")

    def test_symlink_and_hardlinked_inputs_are_refused(self):
        original = self.local_path.read_bytes()
        self.local_path.unlink()
        self.local_path.symlink_to(self.aiven_path)
        self.assert_invalid("VERCEL_ENV_REFUSES_SYMLINKS")
        self.local_path.unlink()
        self.local_path.write_bytes(original)
        self.local_path.chmod(0o600)
        alias = self.root / "linked.env"
        os.link(self.local_path, alias)
        self.assert_invalid("VERCEL_ENV_PRIVATE_OWNED_INPUT_REQUIRED")

    def test_output_must_be_new_under_private_owned_parent(self):
        self.output.mkdir(mode=0o700)
        marker = self.output / "preserved"
        marker.write_text("unchanged")
        with self.assertRaisesRegex(DomainError, "VERCEL_ENV_OUTPUT_ALREADY_EXISTS"):
            self.prepare()
        self.assertEqual(marker.read_text(), "unchanged")
        marker.unlink()
        self.output.rmdir()
        insecure = self.root / "insecure"
        insecure.mkdir(mode=0o755)
        with self.assertRaisesRegex(DomainError, "VERCEL_ENV_PRIVATE_PARENT_REQUIRED"):
            self.prepare(output_directory=insecure / "output")
        self.assertFalse((insecure / "output").exists())

    def test_output_symlink_is_refused_without_touching_destination(self):
        destination = self.root / "untouched"
        destination.mkdir(mode=0o700)
        self.output.symlink_to(destination, target_is_directory=True)
        with self.assertRaisesRegex(DomainError, "VERCEL_ENV_REFUSES_SYMLINKS"):
            self.prepare()
        self.assertEqual(list(destination.iterdir()), [])

    def test_pending_or_misnamed_runtime_credentials_cannot_be_used(self):
        pending = self.runtime_path.with_suffix(".pending.env")
        pending.write_text("fictional pending data")
        pending.chmod(0o600)
        self.assert_invalid("VERCEL_ENV_RUNTIME_CREDENTIALS_REQUIRE_REVIEW")
        pending.unlink()
        self.assert_invalid("VERCEL_ENV_DISTINCT_VERIFIED_SOURCE_FILES_REQUIRED", runtime_env=self.aiven_path)

    def test_duplicate_malformed_or_unvalued_environment_keys_fail_closed(self):
        for extra in ("DB_PASSWORD='duplicate-secret'\n", "UNVALUED_KEY\n", "invalid-key='value'\n", "BROKEN='unterminated\n"):
            with self.subTest(extra=extra):
                self.save_sources()
                with self.local_path.open("a") as output:
                    output.write(extra)
                with self.assertRaises(DomainError):
                    self.prepare()
                self.assertFalse(self.output.exists())

    def test_project_ids_are_distinct_and_provided_together(self):
        for changes in (
            {"admin_project_id": None}, {"scanner_project_id": "wrong"},
            {"scanner_project_id": "prj_fictionalAdmin12345"},
        ):
            with self.subTest(changes=changes):
                self.assert_invalid("VERCEL_ENV_DISTINCT_PROJECT_IDS_REQUIRED", **changes)
        review = self.prepare(admin_project_id=None, scanner_project_id=None)
        self.assertIsNone(review["projects"]["admin"]["id"])
        self.assertIsNone(review["projects"]["scanner"]["id"])

    def test_cli_help_is_offline_and_never_requests_secret_arguments(self):
        output = io.StringIO()
        with patch.object(preparation, "prepare_environment") as prepare, redirect_stdout(output), self.assertRaises(SystemExit) as caught:
            preparation.main(["--help"])
        self.assertEqual(caught.exception.code, 0)
        prepare.assert_not_called()
        self.assertIn("--local-env", output.getvalue())
        self.assertIn("--admin-project-id", output.getvalue())
        self.assertNotIn("--password", output.getvalue())
        self.assertNotIn("--blob-token", output.getvalue())

    def test_cli_outputs_only_safe_review_path_and_offline_status(self):
        arguments = [
            "--local-env", str(self.local_path), "--aiven-env", str(self.aiven_path),
            "--runtime-env", str(self.runtime_path), "--output", str(self.output),
            "--blob-host", "fictionalstore.private.blob.vercel-storage.com",
            "--admin-origin", "https://mccia-meal-admin.vercel.app",
            "--scanner-origin", "https://mccia-meal-scanner.vercel.app",
        ]
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            result = preparation.main(arguments)
        self.assertEqual(result, 0)
        self.assertEqual(errors.getvalue(), "")
        self.assertIn(str(self.output / "review.json"), output.getvalue())
        self.assertIn("not validated locally", output.getvalue())
        for value in (self.runtime["DB_PASSWORD"], "abcdefghijklmnop", self.aiven["QR_ENCRYPTION_KEYS"]):
            self.assertNotIn(value, output.getvalue())

    def test_cli_sanitizes_unexpected_errors_and_unknown_secret_arguments(self):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            result = preparation.main(["--unknown-password", "fictional-secret-do-not-echo"])
        self.assertEqual(result, 1)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(errors.getvalue(), "VERCEL_ENV_INVALID_ARGUMENTS\n")
        arguments = [
            "--local-env", str(self.local_path), "--aiven-env", str(self.aiven_path), "--runtime-env", str(self.runtime_path),
            "--output", str(self.output), "--blob-host", "fictionalstore.private.blob.vercel-storage.com",
            "--admin-origin", "https://admin.example.test", "--scanner-origin", "https://scanner.example.test",
        ]
        errors = io.StringIO()
        with patch.object(preparation, "prepare_environment", side_effect=RuntimeError("fictional-provider-secret")), redirect_stderr(errors):
            self.assertEqual(preparation.main(arguments), 1)
        self.assertEqual(errors.getvalue(), "VERCEL_ENV_PREPARATION_FAILED\n")
        self.assertFalse(self.output.exists())
