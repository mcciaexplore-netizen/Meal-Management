import secrets
from dataclasses import replace
from unittest.mock import Mock
from uuid import uuid4

from fastapi.testclient import TestClient

from meal_management.errors import DomainError
from meal_management.models import ScanResult
from test_api import ApiHarness, ApiTestCase


class PublicScannerApiTests(ApiTestCase):
    def setUp(self):
        self.harness = ApiHarness(application="scanner")
        self.client = self.harness.client
        self.addCleanup(self.client.close)
        self.scanner = Mock()
        self.harness.app.state.scanner = self.scanner
        self.limiter = Mock()
        self.harness.app.state.scanner_limiter = self.limiter
        self.request_id = str(uuid4())
        self.token = secrets.token_urlsafe(32)
        self.result = ScanResult(True, "APPROVED", self.request_id, 25, (31,))
        self.scanner.read.return_value = self.result
        self.scanner.record.return_value = self.result
        self.scanner.latest_request.return_value = {"request": None}

    def session(self, client=None):
        client = client or self.client
        return client.get("/api/scanner/session")

    def headers(self, client=None):
        return {"Origin": self.harness.runtime.app_origin, "X-CSRF-Token": self.session(client).json()["csrf_token"]}

    def post(self, path="/api/scanner/read", body=None, client=None):
        client = client or self.client
        values = {"request_id": self.request_id, "token": self.token} if body is None else body
        return client.post(path, json=values, headers=self.headers(client))

    def details(self):
        return {"company_name": "Example Test Company", "name": "Fictional Visitor",
                "email": "visitor@example.test", "phone": "+91 90000 00000"}

    def test_scanner_bootstrap_needs_no_login_or_database_connection(self):
        response = self.session()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json()), {"scope", "csrf_token"})
        self.assertRegex(response.json()["scope"], r"^[0-9a-f]{64}$")
        self.assertEqual(self.harness.staff.authentication_calls, [])
        self.harness.services.database.transaction.assert_not_called()
        self.scanner.assert_not_called()
        self.scanner.latest_request.assert_not_called()
        cookie = response.headers["set-cookie"]
        self.assertIn("meal_scanner=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=strict", cookie)
        self.assertNotIn("meal_session=", cookie)
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_production_new_device_requires_activation_then_keeps_private_cookie(self):
        activation = secrets.token_urlsafe(32)
        runtime = replace(
            self.harness.runtime,
            environment="production",
            app_origin="https://admin.example.test",
            admin_origin="https://admin.example.test",
            scanner_origin="https://scanner.example.test",
            allowed_hosts=("admin.example.test", "scanner.example.test"),
            cookie_secure=True,
            scanner_enabled=True,
            scanner_activation_secret=activation.encode(),
        )
        harness = ApiHarness(runtime, application="scanner")
        try:
            response = harness.client.get("/api/scanner/session")
            self.assert_error(response, 401, "SCANNER_ACTIVATION_REQUIRED")
            self.assertNotIn("set-cookie", response.headers)
            response = harness.client.post(
                "/api/scanner/activate",
                json={"activation_code": activation},
                headers={"Origin": runtime.scanner_origin},
            )
            self.assertEqual(response.status_code, 200)
            self.assertRegex(response.json()["scope"], r"^[0-9a-f]{64}$")
            self.assertIn("__Host-meal_scanner=", response.headers["set-cookie"])
            self.assertIn("HttpOnly", response.headers["set-cookie"])
            harness.limiter.consume_activation.assert_called_once()
            self.assertEqual(harness.client.get("/api/scanner/session").status_code, 200)
        finally:
            harness.client.close()

    def test_production_rejects_wrong_activation_without_issuing_cookie(self):
        runtime = replace(
            self.harness.runtime,
            environment="production",
            app_origin="https://admin.example.test",
            admin_origin="https://admin.example.test",
            scanner_origin="https://scanner.example.test",
            allowed_hosts=("admin.example.test", "scanner.example.test"),
            cookie_secure=True,
            scanner_enabled=True,
            scanner_activation_secret=secrets.token_urlsafe(32).encode(),
        )
        harness = ApiHarness(runtime, application="scanner")
        try:
            response = harness.client.post(
                "/api/scanner/activate",
                json={"activation_code": secrets.token_urlsafe(32)},
                headers={"Origin": runtime.scanner_origin},
            )
            self.assert_error(response, 401, "SCANNER_ACTIVATION_INVALID")
            self.assertNotIn("set-cookie", response.headers)
            harness.limiter.consume_activation.assert_called_once()
        finally:
            harness.client.close()

    def test_bootstrap_keeps_the_same_browser_scope(self):
        first = self.session().json()
        self.assertEqual(self.session().json(), first)

    def test_cross_site_bootstrap_cannot_replace_scanner_cookie(self):
        for headers in ({"Origin": "https://unrelated.example.test"}, {"Sec-Fetch-Site": "cross-site"},
                        {"Sec-Fetch-Site": "same-site"}, {"Origin": "null"}):
            with self.subTest(headers=headers):
                response = self.client.get("/api/scanner/session", headers=headers)
                self.assert_error(response, 403, "ORIGIN_REJECTED")
                self.assertNotIn("set-cookie", response.headers)
        self.harness.services.database.transaction.assert_not_called()

    def test_same_origin_bootstrap_preserves_scope(self):
        first = self.session().json()["scope"]
        response = self.client.get("/api/scanner/session", headers={
            "Origin": self.harness.runtime.app_origin, "Sec-Fetch-Site": "same-origin",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["scope"], first)

    def test_different_browsers_receive_different_scopes(self):
        with TestClient(self.harness.app, base_url=self.harness.runtime.app_origin) as other:
            self.assertNotEqual(self.session().json()["scope"], self.session(other).json()["scope"])

    def test_employee_scan_uses_browser_context_without_submitted_station(self):
        response = self.post()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["approved"])
        self.assertEqual(response.json()["meal_ids"], [31])
        arguments = self.scanner.read.call_args.args
        self.assertEqual(len(arguments), 3)
        self.assertIsInstance(arguments[0], bytes)
        self.assertEqual(len(arguments[0]), 32)
        self.assertEqual(str(arguments[1]), self.request_id)
        self.assertEqual(arguments[2], self.token)
        self.assertNotIn(self.token, response.text)
        self.assertEqual(self.harness.staff.authentication_calls, [])

    def test_master_read_returns_only_the_committed_meal_result(self):
        expected = {"approved": True, "code": "APPROVED", "request_id": self.request_id,
                    "serving_id": 25, "meal_ids": [31], "duplicate": False}
        self.scanner.read.return_value = expected
        response = self.post()
        self.assertEqual(response.json(), expected)
        self.harness.services.approvals.authorize.assert_not_called()

    def test_scanner_exposes_no_visitor_details_submission_route(self):
        response = self.post("/api/scanner/visitors", {
            "request_id": self.request_id, "token": self.token, "visitor_details": self.details(),
        })
        self.assert_error(response, 404, "NOT_FOUND")

    def test_client_cannot_override_identity_or_service_settings(self):
        for extra in ({"staff_id": 1}, {"waiter_id": 1}, {"roles": ["ADMIN"]}, {"scanner_code": "OTHER"},
                      {"location_id": 5}, {"meal_type_id": 1}, {"quantity": 2}, {"authorization_id": 3}):
            with self.subTest(extra=extra):
                response = self.post(body={"request_id": self.request_id, "token": self.token, **extra})
                self.assert_error(response, 422, "INVALID_INPUT")
        self.scanner.read.assert_not_called()

    def test_scanner_cookie_does_not_grant_admin_or_staff_access(self):
        self.session()
        for path in ("/api/auth/me", "/api/catalog", "/api/employees", "/api/staff", "/api/master-qrs",
                     "/api/email-queue", "/api/reports/meals", "/api/reports/totals", "/api/reports/scans"):
            with self.subTest(path=path):
                self.assert_error(self.client.get(path), 404, "NOT_FOUND")

    def test_scanner_cookie_cannot_be_used_as_staff_session_token(self):
        self.session()
        admin = ApiHarness(self.harness.runtime.for_application("admin"))
        self.addCleanup(admin.client.close)
        admin.client.cookies.set(admin.runtime.session_cookie, self.client.cookies.get("meal_scanner"))
        self.assert_error(admin.client.get("/api/employees"), 401, "AUTHENTICATION_REQUIRED")

    def test_staff_login_does_not_change_scanner_scope_or_identity(self):
        first = self.session().json()["scope"]
        admin = ApiHarness(self.harness.runtime.for_application("admin"))
        self.addCleanup(admin.client.close)
        admin.login("admin")
        self.client.cookies.set(admin.runtime.session_cookie, admin.client.cookies.get(admin.runtime.session_cookie))
        self.post()
        self.assertEqual(self.session().json()["scope"], first)
        self.assertEqual(self.scanner.read.call_args.args[0].hex(), first)

    def test_missing_or_tampered_browser_cookie_cannot_submit(self):
        headers = self.headers()
        self.client.cookies.clear()
        body = {"request_id": self.request_id, "token": self.token}
        self.assert_error(self.client.post("/api/scanner/read", json=body, headers=headers), 401, "SCANNER_BROWSER_REQUIRED")
        self.client.cookies.set("meal_scanner", secrets.token_urlsafe(32))
        self.assert_error(self.client.post("/api/scanner/read", json=body, headers=headers), 401, "SCANNER_BROWSER_REQUIRED")
        self.scanner.read.assert_not_called()

    def test_scanner_requires_its_own_csrf_token(self):
        self.session()
        admin = ApiHarness(self.harness.runtime.for_application("admin"))
        self.addCleanup(admin.client.close)
        headers = {**admin.headers(), "Origin": self.harness.runtime.app_origin}
        response = self.client.post("/api/scanner/read", json={"request_id": self.request_id, "token": self.token},
                                    headers=headers)
        self.assert_error(response, 403, "CSRF_REJECTED")
        self.scanner.read.assert_not_called()

    def test_cross_origin_submission_is_rejected(self):
        response = self.client.post("/api/scanner/read", json={"request_id": self.request_id, "token": self.token},
                                   headers={**self.headers(), "Origin": "https://external.example.test"})
        self.assert_error(response, 403, "ORIGIN_REJECTED")
        self.scanner.read.assert_not_called()

    def test_private_recovery_delegates_browser_owner_not_staff_identity(self):
        self.scanner.result.return_value = {"approved": True, "code": "APPROVED", "request_id": self.request_id,
                                            "serving_id": 25, "meal_ids": [31], "duplicate": True}
        scope = self.session().json()["scope"]
        response = self.client.get("/api/scanner/requests/" + self.request_id + "/result")
        self.assertTrue(response.json()["duplicate"])
        self.assertEqual(self.scanner.result.call_args.args[0].hex(), scope)

    def test_foreign_browser_receipt_failure_is_safe(self):
        self.scanner.result.side_effect = DomainError("SCAN_RECEIPT_NOT_FOUND")
        self.session()
        response = self.client.get("/api/scanner/requests/" + self.request_id + "/result")
        self.assert_error(response, 404, "SCAN_RECEIPT_NOT_FOUND")
        self.assertNotIn("serving_id", response.text)

    def test_invalid_input_is_logged_without_echoing_qr_or_contact_data(self):
        response = self.post(body={"request_id": self.request_id, "token": self.token, "kind": "MASTER"})
        self.assert_error(response, 422, "INVALID_INPUT")
        self.scanner.record_invalid.assert_called_once()
        self.assertNotIn(self.token, response.text)

    def test_limits_apply_once_before_each_valid_or_invalid_submission(self):
        self.post()
        self.assertEqual(self.limiter.consume.call_count, 1)
        self.post(body={"request_id": "invalid", "token": self.token})
        self.assertEqual(self.limiter.consume.call_count, 2)

    def test_scanner_limit_has_its_own_retry_after(self):
        self.limiter.consume.side_effect = DomainError("SCANNER_RATE_LIMITED")
        response = self.post()
        self.assert_error(response, 429, "SCANNER_RATE_LIMITED")
        self.assertEqual(response.headers["retry-after"], "60")
        self.scanner.read.assert_not_called()

    def test_commit_failures_never_claim_approval_or_echo_sql(self):
        self.scanner.read.side_effect = RuntimeError("DB_PASSWORD=private INSERT INTO meals")
        response = self.post()
        self.assert_error(response, 503, "SERVICE_UNAVAILABLE")
        self.assertNotIn("private", response.text)
        self.assertNotIn("INSERT", response.text)
        self.assertNotIn("approved", response.text)

    def test_unknown_commit_result_keeps_original_identifier(self):
        self.scanner.read.return_value = ScanResult(False, "PROCESSING_UNCONFIRMED", self.request_id)
        self.assert_error(self.post(), 503, "PROCESSING_UNCONFIRMED")
        self.assertEqual(str(self.scanner.read.call_args.args[1]), self.request_id)

    def test_setup_failure_is_clear_without_database_details(self):
        self.scanner.read.side_effect = DomainError("SCANNER_NOT_CONFIGURED")
        response = self.post()
        self.assert_error(response, 503, "SCANNER_NOT_CONFIGURED")
        self.assertIn("setup", response.json()["error"]["message"])

    def test_disabled_scanner_does_not_issue_cookie_or_process_input(self):
        harness = ApiHarness(replace(self.harness.runtime, scanner_enabled=False), application="scanner")
        try:
            response = harness.client.get("/api/scanner/session")
            self.assert_error(response, 404, "SCANNER_DISABLED")
            self.assertNotIn("set-cookie", response.headers)
            harness.services.database.transaction.assert_not_called()
        finally:
            harness.client.close()

    def test_secure_scanner_cookie_uses_host_prefix(self):
        runtime = replace(self.harness.runtime, cookie_secure=True, scanner_origin="https://testserver:8001")
        harness = ApiHarness(runtime, application="scanner")
        try:
            response = harness.client.get("/api/scanner/session")
            self.assertEqual(response.status_code, 200)
            cookie = response.headers["set-cookie"]
            self.assertIn("__Host-meal_scanner=", cookie)
            self.assertIn("Secure", cookie)
            self.assertIn("Path=/", cookie)
            self.assertNotIn("Domain=", cookie)
        finally:
            harness.client.close()

    def test_recovery_uses_only_browser_scope_and_returns_minimal_marker(self):
        expected = {"request": {"request_id": self.request_id, "quantity": 1}}
        self.scanner.latest_request.return_value = expected
        scope = self.session().json()["scope"]
        response = self.client.get("/api/scanner/recovery")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), expected)
        self.scanner.latest_request.assert_called_once_with(bytes.fromhex(scope))
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.scanner.read.assert_not_called()
        self.scanner.record.assert_not_called()
        self.scanner.result.assert_not_called()
        self.limiter.consume.assert_not_called()
        self.assertNotIn(self.token, response.text)

    def test_recovery_returns_null_without_inventing_a_completed_or_pending_serving(self):
        self.session()
        response = self.client.get("/api/scanner/recovery")
        self.assertEqual(response.json(), {"request": None})
        self.assertNotIn("approved", response.text)

    def test_recovery_requires_a_valid_scanner_browser_cookie(self):
        response = self.client.get("/api/scanner/recovery")
        self.assert_error(response, 401, "SCANNER_BROWSER_REQUIRED")
        self.scanner.latest_request.assert_not_called()

    def test_recovery_failure_is_unavailable_and_never_claims_empty_history(self):
        self.session()
        self.scanner.latest_request.side_effect = RuntimeError("DB_PASSWORD=private SELECT FROM scan_app_requests")
        response = self.client.get("/api/scanner/recovery")
        self.assert_error(response, 503, "SERVICE_UNAVAILABLE")
        self.assertNotIn("private", response.text)
        self.assertNotIn("SELECT", response.text)
        self.assertNotIn('"request":null', response.text)

    def test_recovery_keeps_each_clients_browser_identity_separate(self):
        first_scope = self.session().json()["scope"]
        self.client.get("/api/scanner/recovery")
        with TestClient(self.harness.app, base_url=self.harness.runtime.app_origin) as other:
            second_scope = self.session(other).json()["scope"]
            other.get("/api/scanner/recovery")
        self.assertNotEqual(first_scope, second_scope)
        self.assertEqual([call.args[0] for call in self.scanner.latest_request.call_args_list], [
            bytes.fromhex(first_scope), bytes.fromhex(second_scope),
        ])


class SeparateApplicationTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        runtime = replace(self.harness.runtime, app_origin="http://testserver:8000", scanner_origin="http://testserver:8001")
        self.admin = ApiHarness(runtime)
        self.scanner = ApiHarness(runtime, application="scanner")
        self.addCleanup(self.admin.client.close)
        self.addCleanup(self.scanner.client.close)

    def test_factories_select_different_origins_without_connecting_to_database(self):
        self.assertEqual(self.admin.app.state.runtime.app_origin, "http://testserver:8000")
        self.assertEqual(self.scanner.app.state.runtime.app_origin, "http://testserver:8001")
        self.admin.services.database.transaction.assert_not_called()
        self.scanner.services.database.transaction.assert_not_called()
        self.assertFalse(hasattr(self.admin.app.state, "scanner"))
        self.assertFalse(hasattr(self.scanner.app.state, "storage"))
        self.assertFalse(hasattr(self.scanner.app.state, "queries"))

    def test_admin_application_exposes_safe_scanner_location_and_photo_limit_metadata(self):
        response = self.admin.client.get("/api/application")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"scanner_url": "http://testserver:8001", "max_photo_bytes": 5 * 1024 * 1024})
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.admin.services.database.transaction.assert_not_called()
        self.assert_error(self.scanner.client.get("/api/application"), 404, "NOT_FOUND")

    def test_admin_has_no_public_scanner_routes_or_scanner_page(self):
        for method, path in (
            ("GET", "/scan"), ("GET", "/scan/"), ("GET", "/api/scanner/session"),
            ("POST", "/api/scanner/read"), ("POST", "/api/scanner/visitors"),
            ("GET", "/api/scanner/recovery"),
            ("GET", "/api/scanner/requests/" + str(uuid4()) + "/result"),
        ):
            with self.subTest(path=path):
                response = self.admin.client.request(method, path, headers={"Origin": self.admin.runtime.app_origin})
                self.assert_error(response, 404, "NOT_FOUND")
        self.admin.services.database.transaction.assert_not_called()

    def test_scanner_has_no_authentication_admin_or_legacy_staff_routes(self):
        for method, path in (
            ("GET", "/api/auth/csrf"), ("POST", "/api/auth/login"), ("POST", "/api/auth/logout"),
            ("GET", "/api/auth/me"), ("GET", "/api/employees"), ("POST", "/api/employees"),
            ("GET", "/api/staff"), ("GET", "/api/catalog"), ("GET", "/api/master-qrs"),
            ("GET", "/api/reports/meals"), ("GET", "/api/reports/totals"), ("GET", "/api/reports/scans"),
            ("GET", "/api/email-queue"), ("POST", "/api/visitor-authorizations"),
            ("POST", "/api/scan-app/read"), ("POST", "/api/scans"),
            ("GET", "/api/development/test-data"), ("GET", "/docs"), ("GET", "/openapi.json"),
        ):
            with self.subTest(path=path):
                response = self.scanner.client.request(method, path, headers={"Origin": self.scanner.runtime.app_origin})
                self.assert_error(response, 404, "NOT_FOUND")
        self.scanner.services.database.transaction.assert_not_called()

    def test_scanner_route_inventory_is_limited_to_its_application(self):
        paths = {route.path for route in self.scanner.app.routes}
        self.assertTrue(paths.issubset({
            "/", "/assets", "/health/live", "/health/ready", "/api/scanner/session",
            "/api/scanner/activate",
            "/api/scanner/read", "/api/scanner/visitors", "/api/scanner/requests/{request_id}/result",
            "/api/scanner/recovery",
        }))

    def test_admin_port_cannot_issue_or_submit_scanner_browser_requests(self):
        response = self.scanner.client.get("/api/scanner/session", headers={"Origin": self.admin.runtime.app_origin})
        self.assert_error(response, 403, "ORIGIN_REJECTED")
        self.assertNotIn("set-cookie", response.headers)
        session = self.scanner.client.get("/api/scanner/session").json()
        response = self.scanner.client.post("/api/scanner/read", json={"request_id": str(uuid4()), "token": secrets.token_urlsafe(32)}, headers={
            "Origin": self.admin.runtime.app_origin, "X-CSRF-Token": session["csrf_token"],
        })
        self.assert_error(response, 403, "ORIGIN_REJECTED")
        self.scanner.services.database.transaction.assert_not_called()

    def test_scanner_port_cannot_submit_admin_mutations(self):
        self.admin.login()
        response = self.admin.client.post("/api/auth/logout", headers={
            **self.admin.headers(), "Origin": self.scanner.runtime.app_origin,
        })
        self.assert_error(response, 403, "ORIGIN_REJECTED")
        self.assertEqual(self.admin.client.get("/api/auth/me").status_code, 200)

    def test_both_application_liveness_checks_work_without_database_connections(self):
        for harness in (self.admin, self.scanner):
            with self.subTest(origin=harness.runtime.app_origin):
                response = harness.client.get("/health/live")
                self.assertEqual(response.json(), {"status": "ok"})
                harness.services.database.transaction.assert_not_called()
