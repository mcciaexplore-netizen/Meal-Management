import secrets
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from unittest.mock import Mock
from uuid import uuid4

from meal_management.errors import DomainError
from meal_management.models import ScanResult
from test_api import ApiHarness, ApiTestCase


class ScannerPhoneAdminApiTests(ApiTestCase):
    def setUp(self):
        self.harness = ApiHarness()
        self.client = self.harness.client
        self.addCleanup(self.client.close)
        self.phones = self.harness.phones
        self.phones.list.return_value = {"phones": [], "codes": [], "latest_active_date": "2027-04-25", "today": "2026-10-25"}
        self.phones.create_code.return_value = {
            "id": 7, "label": "Counter 2", "code": "ABCD-EFGH", "code_expires_at": "2026-10-26T10:00:00",
            "active_until": "2026-11-30T18:30:00", "active_until_date": "2026-11-30",
        }
        self.phones.revoke.return_value = {"id": 7, "revoked": True}

    def test_admin_lists_generates_and_revokes_through_the_service(self):
        self.assertEqual(self.harness.login().status_code, 200)
        response = self.client.get("/api/scanner-phones")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["latest_active_date"], "2027-04-25")
        response = self.harness.post("/api/scanner-phones", json={"label": "Counter 2", "active_until": "2026-11-30"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["code"], "ABCD-EFGH")
        self.assertEqual(response.headers["cache-control"], "no-store")
        _, label, active_until = self.phones.create_code.call_args.args
        self.assertEqual((label, active_until), ("Counter 2", date(2026, 11, 30)))
        response = self.harness.post("/api/scanner-phones/7/revoke")
        self.assertEqual(response.json(), {"id": 7, "revoked": True})
        self.assertEqual(self.phones.revoke.call_args.args[1], 7)

    def test_listing_never_contains_a_code_field(self):
        self.phones.list.return_value = {
            "phones": [{"id": 1, "label": "Counter", "source": "CODE", "activated_at": "2026-10-25T10:00:00",
                        "active_until": "2026-11-30T18:30:00", "active_until_date": "2026-11-30"}],
            "codes": [], "latest_active_date": "2027-04-25", "today": "2026-10-25",
        }
        self.harness.login()
        body = self.client.get("/api/scanner-phones").json()
        self.assertNotIn("code", body["phones"][0])

    def test_generation_input_requires_a_calendar_date_and_bounded_label(self):
        self.harness.login()
        for body in ({"label": "Counter", "active_until": "2026-11-30T00:00:00Z"},
                     {"label": "Counter", "active_until": "30/11/2026"},
                     {"label": "Counter", "active_until": 20261130},
                     {"label": "Counter", "active_until": "2026-02-30"},
                     {"active_until": "2026-11-30"},
                     {"label": "x" * 101, "active_until": "2026-11-30"},
                     {"label": "Counter", "active_until": "2026-11-30", "code": "ABCD-EFGH"}):
            with self.subTest(body=body):
                self.assert_error(self.harness.post("/api/scanner-phones", json=body), 422, "INVALID_INPUT")
        self.assert_error(self.harness.post("/api/scanner-phones/0/revoke"), 422, "INVALID_INPUT")
        self.phones.create_code.assert_not_called()
        self.phones.revoke.assert_not_called()

    def test_service_errors_map_to_clear_statuses(self):
        self.harness.login()
        self.phones.create_code.side_effect = DomainError("ACTIVE_UNTIL_OUT_OF_RANGE")
        response = self.harness.post("/api/scanner-phones", json={"label": "Counter", "active_until": "2028-01-01"})
        self.assert_error(response, 400, "ACTIVE_UNTIL_OUT_OF_RANGE")
        self.assertIn("six months", response.json()["error"]["message"])
        self.phones.revoke.side_effect = DomainError("SCANNER_PHONE_NOT_FOUND")
        self.assert_error(self.harness.post("/api/scanner-phones/9/revoke"), 404, "SCANNER_PHONE_NOT_FOUND")
        self.phones.revoke.side_effect = DomainError("SCANNER_PHONE_ALREADY_REVOKED")
        self.assert_error(self.harness.post("/api/scanner-phones/9/revoke"), 409, "SCANNER_PHONE_ALREADY_REVOKED")

    def test_waiters_and_anonymous_users_cannot_manage_scanner_access(self):
        calls = (
            lambda: self.client.get("/api/scanner-phones"),
            lambda: self.harness.post("/api/scanner-phones", json={"label": "Counter", "active_until": "2026-11-30"}),
            lambda: self.harness.post("/api/scanner-phones/7/revoke"),
        )
        for call in calls:
            self.assert_error(call(), 401, "AUTHENTICATION_REQUIRED")
        self.harness.login("waiter")
        for call in calls:
            self.assert_error(call(), 403, "ROLE_REQUIRED")
        self.phones.list.assert_not_called()
        self.phones.create_code.assert_not_called()
        self.phones.revoke.assert_not_called()

    def test_generation_requires_csrf(self):
        self.harness.login()
        response = self.client.post("/api/scanner-phones", json={"label": "Counter", "active_until": "2026-11-30"},
                                    headers={"Origin": self.harness.runtime.app_origin})
        self.assert_error(response, 403, "CSRF_REJECTED")
        self.phones.create_code.assert_not_called()


class ScannerPhoneScannerApiTests(ApiTestCase):
    def production(self, secret=True):
        base = ApiHarness(application="scanner").runtime
        runtime = replace(
            base,
            environment="production",
            app_origin="https://admin.example.test",
            admin_origin="https://admin.example.test",
            scanner_origin="https://scanner.example.test",
            allowed_hosts=("admin.example.test", "scanner.example.test"),
            cookie_secure=True,
            scanner_enabled=True,
            scanner_activation_secret=self.secret.encode() if secret else None,
        )
        harness = ApiHarness(runtime, application="scanner")
        self.addCleanup(harness.client.close)
        harness.app.state.scanner = Mock()
        harness.app.state.scanner.read.return_value = ScanResult(True, "APPROVED", str(uuid4()), 25, (31,))
        return harness

    def setUp(self):
        self.secret = secrets.token_urlsafe(32)

    def activate(self, harness, code):
        return harness.client.post("/api/scanner/activate", json={"activation_code": code},
                                   headers={"Origin": harness.runtime.scanner_origin})

    def read(self, harness):
        session = harness.client.get("/api/scanner/session")
        headers = {"Origin": harness.runtime.scanner_origin, "X-CSRF-Token": session.json().get("csrf_token", "")}
        return harness.client.post("/api/scanner/read", json={"request_id": str(uuid4()), "token": "t" * 43, "meal_type_id": 5}, headers=headers)

    def test_admin_code_activates_this_phone_and_binds_its_browser_identity(self):
        harness = self.production()
        response = self.activate(harness, "abcd-efgh")
        self.assertEqual(response.status_code, 200, response.text)
        code, identity = harness.phones.activate_code.call_args.args
        self.assertEqual(code, "ABCDEFGH")
        self.assertEqual(identity.hex(), response.json()["scope"])
        harness.phones.register_secret_activation.assert_not_called()
        harness.limiter.consume_activation.assert_called_once()
        self.assertIn("__Host-meal_scanner=", response.headers["set-cookie"])
        session = harness.client.get("/api/scanner/session")
        self.assertEqual(session.status_code, 200)
        self.assertEqual(harness.phones.authorize.call_args.args, (identity, True))

    def test_rejected_code_issues_no_cookie(self):
        harness = self.production()
        harness.phones.activate_code.side_effect = DomainError("SCANNER_ACTIVATION_INVALID")
        response = self.activate(harness, "ABCD-EFGH")
        self.assert_error(response, 401, "SCANNER_ACTIVATION_INVALID")
        self.assertNotIn("set-cookie", response.headers)
        harness.limiter.consume_activation.assert_called_once()

    def test_emergency_secret_still_activates_and_registers_a_phone(self):
        harness = self.production()
        response = self.activate(harness, self.secret)
        self.assertEqual(response.status_code, 200, response.text)
        identity = harness.phones.register_secret_activation.call_args.args[0]
        self.assertEqual(identity.hex(), response.json()["scope"])
        harness.phones.activate_code.assert_not_called()

    def test_without_the_secret_only_admin_codes_activate_and_legacy_cookies_are_not_adopted(self):
        harness = self.production(secret=False)
        self.assert_error(self.activate(harness, self.secret), 401, "SCANNER_ACTIVATION_INVALID")
        harness.phones.register_secret_activation.assert_not_called()
        self.assertEqual(self.activate(harness, "ABCD-EFGH").status_code, 200)
        harness.client.get("/api/scanner/session")
        self.assertIs(harness.phones.authorize.call_args.args[1], False)

    def test_revoked_or_expired_phone_is_sent_back_to_activation_and_cannot_scan(self):
        harness = self.production()
        self.assertEqual(self.activate(harness, "ABCD-EFGH").status_code, 200)
        csrf = harness.client.get("/api/scanner/session").json()["csrf_token"]
        harness.phones.authorize.side_effect = DomainError("SCANNER_ACTIVATION_REQUIRED")
        response = harness.client.post(
            "/api/scanner/read", json={"request_id": str(uuid4()), "token": "t" * 43, "meal_type_id": 5},
            headers={"Origin": harness.runtime.scanner_origin, "X-CSRF-Token": csrf},
        )
        self.assert_error(response, 401, "SCANNER_ACTIVATION_REQUIRED")
        harness.app.state.scanner.read.assert_not_called()
        self.assertEqual(self.activate(harness, "ABCD-EFGH").status_code, 200)
        response = harness.client.get("/api/scanner/session")
        self.assert_error(response, 401, "SCANNER_ACTIVATION_REQUIRED")
        self.assertIn("__Host-meal_scanner=", response.headers["set-cookie"])
        self.assertTrue("Max-Age=0" in response.headers["set-cookie"] or "expires=" in response.headers["set-cookie"].lower())

    def test_active_phone_can_scan_and_cookie_lifetime_stops_at_active_until(self):
        harness = self.production()
        until = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=2)
        harness.phones.activate_code.return_value = {"id": 2, "active_until": until}
        response = self.activate(harness, "ABCD-EFGH")
        max_age = int(response.headers["set-cookie"].split("Max-Age=")[1].split(";")[0])
        self.assertTrue(7100 <= max_age <= 7200, max_age)
        self.assertEqual(self.read(harness).status_code, 200)
        harness.app.state.scanner.read.assert_called_once()

    def test_development_scanner_does_not_require_phone_records(self):
        harness = ApiHarness(application="scanner")
        self.addCleanup(harness.client.close)
        self.assertEqual(harness.client.get("/api/scanner/session").status_code, 200)
        self.assertEqual(harness.client.get("/api/scanner/session").status_code, 200)
        harness.phones.authorize.assert_not_called()
