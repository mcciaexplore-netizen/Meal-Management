import base64
import secrets
import stat
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from meal_management.errors import ConfigurationError
from meal_management.scanner_access import ScannerBrowser
from meal_management.vercel_runtime import VercelRequestMiddleware, create_vercel_app, scanner_networks, vercel_configuration


def environment(**changes):
    values = {
        "VERCEL": "1", "APP_ENV": "production",
        "APP_ORIGIN": "https://admin.example.test", "SCANNER_ORIGIN": "https://scanner.example.test",
        "APP_CSRF_SECRET": "c" * 32, "LOGIN_RATE_SECRET": "r" * 32,
        "PHOTO_BACKEND": "vercel_blob", "BLOB_READ_WRITE_TOKEN": "vercel_blob_rw_teststore_" + "t" * 32,
        "BLOB_STORE_HOST": "teststore.private.blob.vercel-storage.com",
        "EMAIL_BACKEND": "gmail", "EMAIL_SENDER": "meals@example.test", "GMAIL_APP_PASSWORD": "abcdefghijklmnop",
        "EMAIL_SEND_ENABLED": "false", "EMAIL_AUTO_SEND_ENABLED": "false",
        "DB_HOST": "mysql-test.aivencloud.com", "DB_NAME": "defaultdb", "DB_USER": "meal_runtime",
        "DB_PASSWORD": "private-db-test-password", "DB_SSL_CA_PEM": "-----BEGIN CERTIFICATE-----\nfixture\n-----END CERTIFICATE-----",
        "QR_ENCRYPTION_KEYS": base64.urlsafe_b64encode(b"k" * 32).decode(),
    }
    values.update(changes)
    return values


class VercelConfigurationTests(unittest.TestCase):
    def configure(self, application="admin", **changes):
        with patch("meal_management.vercel_runtime.ssl.create_default_context"):
            result = vercel_configuration(application, environment(**changes))
        self.addCleanup(Path(result[0].db_ssl_ca).unlink, missing_ok=True)
        return result

    def test_explicit_remote_settings_have_no_database_or_email_side_effects(self):
        with patch("meal_management.database.Database._connect", side_effect=AssertionError("Unexpected connection")):
            settings, runtime, networks = self.configure()
        self.assertEqual(runtime.max_photo_bytes, 4000000)
        self.assertEqual(runtime.email_process_limit, 1)
        self.assertFalse(runtime.email_auto_send_enabled)
        self.assertFalse(runtime.email_send_enabled)
        self.assertEqual(networks, ())
        self.assertEqual(stat.S_IMODE(Path(settings.db_ssl_ca).stat().st_mode), 0o600)
        self.assertTrue(Path(settings.db_ssl_ca).read_text().startswith("-----BEGIN CERTIFICATE-----"))

    def test_requires_platform_production_and_fixed_project_role(self):
        for values, code in (
            ({"VERCEL": "0"}, "VERCEL_RUNTIME_REQUIRED"),
            ({"APP_ENV": "development"}, "VERCEL_REQUIRES_PRODUCTION_CONFIGURATION"),
            ({"MEAL_APPLICATION": "scanner"}, "VERCEL_APPLICATION_MISMATCH"),
        ):
            with self.subTest(values=values), self.assertRaisesRegex(ConfigurationError, code):
                self.configure(**values)

    def test_rejects_background_delivery_and_oversized_limits(self):
        for values, code in (
            ({"EMAIL_AUTO_SEND_ENABLED": "true", "EMAIL_SEND_ENABLED": "true"}, "VERCEL_BACKGROUND_EMAIL_NOT_SUPPORTED"),
            ({"EMAIL_PROCESS_LIMIT": "10"}, "VERCEL_EMAIL_PROCESS_LIMIT_MUST_BE_ONE"),
            ({"MAX_PHOTO_BYTES": "5000000"}, "VERCEL_PHOTO_LIMIT_EXCEEDED"),
        ):
            with self.subTest(values=values), self.assertRaisesRegex(ConfigurationError, code):
                self.configure(**values)

    def test_scanner_requires_device_activation_secret_only_when_enabled(self):
        _, runtime, networks = self.configure("scanner")
        self.assertFalse(runtime.scan_app_enabled)
        self.assertEqual(networks, ())
        with self.assertRaisesRegex(ConfigurationError, "MISSING_SETTING_SCANNER_ACTIVATION_SECRET"):
            self.configure("scanner", SCAN_APP_ENABLED="true")
        _, runtime, networks = self.configure("scanner", SCAN_APP_ENABLED="true", SCANNER_ACTIVATION_SECRET="s" * 32)
        self.assertTrue(runtime.scan_app_enabled)
        self.assertEqual(networks, ())

    def test_short_activation_code_can_activate_with_production_configuration(self):
        for length in (12, 16, 32):
            code = secrets.token_urlsafe(32)[:length]
            with self.subTest(length=length):
                _, runtime, _ = self.configure("scanner", SCAN_APP_ENABLED="true", SCANNER_ACTIVATION_SECRET=code)
                browser = ScannerBrowser(runtime.csrf_secret, runtime.scanner_activation_secret)
                browser.verify_activation(code)
                cookie = browser.issue()
                self.assertTrue(browser.valid(cookie))

    def test_activation_minimum_does_not_relax_signing_secret_requirements(self):
        for key, length in (("SCANNER_ACTIVATION_SECRET", 11), ("APP_CSRF_SECRET", 31), ("LOGIN_RATE_SECRET", 31)):
            with self.subTest(key=key), self.assertRaisesRegex(ConfigurationError, "INVALID_SETTING_" + key):
                self.configure("scanner", **{key: secrets.token_urlsafe(32)[:length]})

    def test_network_configuration_rejects_missing_malformed_and_unrestricted_ranges(self):
        for value in (None, "", "0.0.0.0/0", "::/0", "127.0.0.1/32", "203.0.113.7/24", "secret-password", "203.0.113.1/32,", ",".join(["203.0.113.9/32"] * 17)):
            with self.subTest(value=value), self.assertRaises(ConfigurationError) as caught:
                scanner_networks(value)
            self.assertNotIn("secret-password", str(caught.exception))

    def test_requires_restricted_account_and_hosted_database(self):
        for user in ("root", "avnadmin", "AVNADMIN"):
            with self.subTest(user=user), self.assertRaisesRegex(ConfigurationError, "RESTRICTED_DATABASE_ACCOUNT"):
                self.configure(DB_USER=user)
        with self.assertRaisesRegex(ConfigurationError, "VERCEL_REQUIRES_AIVEN_DATABASE"):
            self.configure(DB_HOST="localhost")

    def test_invalid_certificate_is_rejected_without_file_or_connection(self):
        with patch("meal_management.vercel_runtime.tempfile.mkstemp") as temporary:
            with self.assertRaisesRegex(ConfigurationError, "INVALID_SETTING_DB_SSL_CA_PEM"):
                vercel_configuration("admin", environment())
            temporary.assert_not_called()

    def test_missing_certificate_identifies_setting_without_echoing_secrets(self):
        with self.assertRaisesRegex(ConfigurationError, "MISSING_SETTING_DB_SSL_CA_PEM"):
            self.configure(DB_SSL_CA_PEM="")

    def test_real_factories_keep_routes_separate_without_connecting(self):
        headers = {"x-forwarded-proto": "https", "x-vercel-forwarded-for": "203.0.113.9"}
        with patch("meal_management.vercel_runtime.ssl.create_default_context"), patch("meal_management.database.Database._connect", side_effect=AssertionError("Unexpected connection")):
            for application in ("admin", "scanner"):
                with self.subTest(application=application):
                    app = create_vercel_app(application, environment(SCAN_APP_ENABLED="true", SCANNER_ACTIVATION_SECRET="s" * 32))
                    self.addCleanup(Path(app.state.services.database.settings.db_ssl_ca).unlink, missing_ok=True)
                    with TestClient(app, base_url="https://" + application + ".example.test", headers=headers) as client:
                        self.assertEqual(client.get("/health/live").status_code, 200)
                        expected = 401 if application == "admin" else 404
                        self.assertEqual(client.get("/api/employees").status_code, expected)
                        expected = 404 if application == "admin" else 401
                        self.assertEqual(client.get("/api/scanner/session").status_code, expected)


class VercelRequestTests(unittest.TestCase):
    def client(self, application="scanner", cidrs="203.0.113.9/32,2001:db8::9/128"):
        app = FastAPI()

        @app.get("/api/scanner/session")
        def identity(request: Request):
            return {"address": request.client.host, "scheme": request.scope["scheme"]}

        app.add_middleware(VercelRequestMiddleware, application=application, networks=scanner_networks(cidrs))
        return TestClient(app)

    def test_uses_platform_identity_and_https_not_untrusted_forwarded_for(self):
        with self.client() as client:
            response = client.get("/api/scanner/session", headers={
                "x-forwarded-proto": "https", "x-vercel-forwarded-for": "203.0.113.9", "x-forwarded-for": "198.51.100.1",
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"address": "203.0.113.9", "scheme": "https"})

    def test_scanner_requests_can_move_between_networks(self):
        with self.client() as client:
            response = client.get("/api/scanner/session", headers={"x-forwarded-proto": "https", "x-vercel-forwarded-for": "198.51.100.1"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["address"], "198.51.100.1")

    def test_admin_remains_reachable_outside_scanner_network(self):
        with self.client("admin") as client:
            response = client.get("/api/scanner/session", headers={"x-forwarded-proto": "https", "x-vercel-forwarded-for": "198.51.100.1"})
        self.assertEqual(response.status_code, 200)

    def test_rejects_spoofed_lists_duplicates_and_missing_proxy_headers(self):
        cases = (
            [], [("x-forwarded-proto", "http"), ("x-vercel-forwarded-for", "203.0.113.9")],
            [("x-forwarded-proto", "https"), ("x-forwarded-for", "203.0.113.9")],
            [("x-forwarded-proto", "https"), ("x-vercel-forwarded-for", "203.0.113.9,198.51.100.1")],
            [("x-forwarded-proto", "https"), ("x-vercel-forwarded-for", "203.0.113.9"), ("x-vercel-forwarded-for", "198.51.100.1")],
        )
        with self.client() as client:
            for headers in cases:
                with self.subTest(headers=headers):
                    self.assertEqual(client.get("/api/scanner/session", headers=headers).status_code, 400)

    def test_ipv6_and_mapped_ipv4_addresses_are_checked(self):
        with self.client() as client:
            for address in ("2001:db8::9", "::ffff:203.0.113.9"):
                with self.subTest(address=address):
                    response = client.get("/api/scanner/session", headers={"x-forwarded-proto": "https", "x-vercel-forwarded-for": address})
                    self.assertEqual(response.status_code, 200)
