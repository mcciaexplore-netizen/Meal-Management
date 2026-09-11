import secrets
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from meal_management.errors import ConfigurationError, DomainError
from meal_management.runtime import RuntimeSettings
from meal_management.scanner_access import ScannerBrowser, ScannerLimiter
from test_http_security import LimiterDatabase
from test_runtime_storage import environment


class ScannerBrowserTests(unittest.TestCase):
    def setUp(self):
        self.activation = secrets.token_urlsafe(32)
        self.browser = ScannerBrowser(secrets.token_bytes(32), self.activation.encode())

    def test_private_cookie_produces_distinct_scope_and_csrf(self):
        cookie = self.browser.issue()
        self.assertTrue(self.browser.valid(cookie))
        self.assertEqual(len(self.browser.identity(cookie)), 32)
        self.assertEqual(len(self.browser.csrf(cookie)), 64)
        self.assertNotEqual(self.browser.identity(cookie).hex(), self.browser.csrf(cookie))
        self.browser.verify_csrf(cookie, self.browser.csrf(cookie))

    def test_browser_scopes_are_random_and_stable(self):
        first, second = self.browser.issue(), self.browser.issue()
        self.assertNotEqual(first, second)
        self.assertEqual(self.browser.identity(first), self.browser.identity(first))
        self.assertNotEqual(self.browser.identity(first), self.browser.identity(second))

    def test_unsigned_tampered_and_staff_session_cookies_are_rejected(self):
        cookie = self.browser.issue()
        for invalid in (None, "", secrets.token_urlsafe(32), cookie[:-1] + ("a" if cookie[-1] != "a" else "b"),
                        "../" + cookie, cookie + ".", "a" * 10000):
            with self.subTest(value_type=type(invalid).__name__):
                self.assertFalse(self.browser.valid(invalid))
                with self.assertRaisesRegex(DomainError, "SCANNER_BROWSER_REQUIRED"):
                    self.browser.identity(invalid)

    def test_csrf_cannot_move_between_browsers_or_use_invalid_characters(self):
        first, second = self.browser.issue(), self.browser.issue()
        for supplied in (None, "", "é" * 64, "a" * 63, self.browser.csrf(second)):
            with self.subTest(value_type=type(supplied).__name__):
                with self.assertRaisesRegex(DomainError, "CSRF_REJECTED"):
                    self.browser.verify_csrf(first, supplied)

    def test_secret_rotation_rejects_old_cookie(self):
        cookie = self.browser.issue()
        rotated = ScannerBrowser(secrets.token_bytes(32))
        self.assertFalse(rotated.valid(cookie))

    def test_activation_code_is_checked_without_becoming_browser_identity(self):
        self.browser.verify_activation(self.activation)
        for value in (None, "", "short", self.activation + "x", "x" * 257):
            with self.subTest(value_type=type(value).__name__), self.assertRaisesRegex(DomainError, "SCANNER_ACTIVATION_INVALID"):
                self.browser.verify_activation(value)

    def test_missing_activation_configuration_cannot_enroll(self):
        with self.assertRaisesRegex(DomainError, "SCANNER_ACTIVATION_REQUIRED"):
            ScannerBrowser(secrets.token_bytes(32)).verify_activation(self.activation)


class ScannerRuntimeTests(unittest.TestCase):
    def test_development_enables_scanner_without_new_credentials(self):
        settings = RuntimeSettings.from_env(environment())
        self.assertTrue(settings.scan_app_enabled)

    def test_scanner_can_be_explicitly_disabled(self):
        self.assertFalse(RuntimeSettings.from_env(environment(SCAN_APP_ENABLED="false")).scan_app_enabled)

    def test_production_requires_explicit_public_scanner_setting(self):
        values = environment(
            APP_ENV="production", APP_ORIGIN="https://meal.example.test",
            PHOTO_BACKEND="s3", EMAIL_BACKEND="ses", AWS_REGION="ap-south-1",
            PHOTO_S3_BUCKET="example-private-photos", EMAIL_SENDER="meals@example.test",
        )
        self.assertFalse(RuntimeSettings.from_env(values).scan_app_enabled)
        self.assertTrue(RuntimeSettings.from_env({**values, "SCAN_APP_ENABLED": "true"}).scan_app_enabled)

    def test_direct_runtime_production_default_is_disabled(self):
        settings = RuntimeSettings(csrf_secret=b"a" * 32, login_rate_secret=b"b" * 32, environment="production")
        self.assertFalse(settings.scan_app_enabled)

    def test_invalid_scanner_configuration_is_rejected(self):
        for key, value in (("SCAN_APP_ENABLED", "maybe"), ("SCAN_REQUEST_LIMIT", "0"),
                           ("SCAN_IP_LIMIT", "10001"), ("SCAN_WINDOW_SECONDS", "invalid")):
            with self.subTest(key=key):
                with self.assertRaises(ConfigurationError):
                    RuntimeSettings.from_env(environment(**{key: value}))


class ScannerLimiterTests(unittest.TestCase):
    def setUp(self):
        self.db = LimiterDatabase()
        self.settings = RuntimeSettings(
            csrf_secret=secrets.token_bytes(32), login_rate_secret=secrets.token_bytes(32),
            scanner_request_limit=2, scanner_ip_limit=3, scanner_window_seconds=60,
        )
        self.limiter = ScannerLimiter(self.db, self.settings)
        self.identity = secrets.token_bytes(32)

    def test_browser_limit_is_shared_across_addresses(self):
        self.limiter.consume(self.identity, "192.0.2.1")
        self.limiter.consume(self.identity, "192.0.2.2")
        with self.assertRaisesRegex(DomainError, "SCANNER_RATE_LIMITED"):
            self.limiter.consume(self.identity, "192.0.2.3")
        self.assertNotIn("192.0.2", repr(self.db.rows))

    def test_ip_limit_applies_across_new_browser_cookies(self):
        for _ in range(3):
            self.limiter.consume(secrets.token_bytes(32), "192.0.2.1")
        with self.assertRaisesRegex(DomainError, "SCANNER_RATE_LIMITED"):
            self.limiter.consume(secrets.token_bytes(32), "192.0.2.1")

    def test_expired_window_resets(self):
        for _ in range(2):
            self.limiter.consume(self.identity, "192.0.2.1")
        self.db.clock += timedelta(seconds=60)
        self.limiter.consume(self.identity, "192.0.2.1")
        self.assertEqual({row["attempts"] for row in self.db.rows.values()}, {1})

    def test_commit_failure_does_not_grant_a_request(self):
        self.db.fail_commit = True
        with self.assertRaises(RuntimeError):
            self.limiter.consume(self.identity, "192.0.2.1")
        self.assertEqual(self.db.rows, {})

    def test_parallel_requests_share_committed_limits(self):
        def consume(number):
            try:
                self.limiter.consume(self.identity, "192.0.2." + str(number))
                return True
            except DomainError:
                return False
        with ThreadPoolExecutor(max_workers=6) as pool:
            result = list(pool.map(consume, range(6)))
        self.assertEqual(sum(result), 2)

    def test_activation_attempts_have_a_small_per_address_limit(self):
        for _ in range(5):
            self.limiter.consume_activation("192.0.2.10")
        with self.assertRaisesRegex(DomainError, "SCANNER_RATE_LIMITED"):
            self.limiter.consume_activation("192.0.2.10")
        self.limiter.consume_activation("192.0.2.11")
