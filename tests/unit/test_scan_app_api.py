import secrets
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import uuid4

from meal_management.models import ScanResult
from test_api import ApiHarness, ApiTestCase


class ScanAppApiTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.harness.login("waiter")

    def read_body(self, **changes):
        body = self.harness.scan_body()
        body.pop("quantity")
        body.update(changes)
        return body

    def visitor_details(self, **changes):
        values = {
            "company_name": "Example Test Company",
            "name": "Fictional Visitor",
            "email": "visitor@example.test",
            "phone": "+91 90000 00000",
        }
        values.update(changes)
        return values

    def test_employee_read_commits_one_meal_using_authenticated_identity(self):
        body = self.read_body()
        response = self.harness.post("/api/scan-app/read", json=body)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["approved"])
        self.assertEqual(len(response.json()["meal_ids"]), 1)
        actor_id, scan = self.harness.meals.reads[-1]
        self.assertEqual(actor_id, 2)
        self.assertEqual(scan.quantity, 1)
        self.assertIsNone(scan.authorization_id)
        self.assertIsNone(scan.visitor_details)
        self.assertNotIn(body["token"], response.text)

    def test_employee_retry_replays_committed_meal(self):
        body = self.read_body()
        first = self.harness.post("/api/scan-app/read", json=body).json()
        second = self.harness.post("/api/scan-app/read", json=body).json()
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["meal_ids"], second["meal_ids"])
        self.assertEqual(len(self.harness.meals.meals), 1)

    def test_new_employee_request_records_next_meal(self):
        body = self.read_body()
        self.harness.post("/api/scan-app/read", json=body)
        self.harness.post("/api/scan-app/read", json={**body, "request_id": str(uuid4())})
        self.assertEqual(len(self.harness.meals.meals), 2)

    def test_master_read_requests_details_without_approval_or_meal(self):
        body = self.read_body(token=self.harness.meals.master_token)
        response = self.harness.post("/api/scan-app/read", json=body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "kind": "MASTER", "next": "VISITOR_DETAILS", "request_id": body["request_id"],
        })
        self.assertEqual(self.harness.meals.meals, [])
        self.harness.services.approvals.authorize.assert_not_called()

    def test_master_form_records_one_meal_without_admin_approval(self):
        body = self.read_body(token=self.harness.meals.master_token)
        self.harness.post("/api/scan-app/read", json=body)
        response = self.harness.post("/api/scans", json={**body, "visitor_details": self.visitor_details()})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["approved"])
        self.assertEqual(len(response.json()["meal_ids"]), 1)
        staff_id, scan = self.harness.meals.scans[-1]
        self.assertEqual(staff_id, 2)
        self.assertEqual(scan.visitor_details, self.visitor_details())
        self.assertIsNone(scan.authorization_id)
        self.harness.services.approvals.authorize.assert_not_called()

    def test_master_retry_preserves_original_committed_result(self):
        body = self.read_body(token=self.harness.meals.master_token)
        body["visitor_details"] = self.visitor_details()
        first = self.harness.post("/api/scans", json=body).json()
        second = self.harness.post("/api/scans", json=body).json()
        self.assertTrue(first["approved"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["meal_ids"], second["meal_ids"])
        self.assertEqual(len(self.harness.meals.meals), 1)

    def test_master_prepared_read_binds_form_submission_to_original_caller(self):
        body = self.read_body(token=self.harness.meals.master_token)
        self.harness.post("/api/scan-app/read", json=body)
        self.harness.login("second")
        response = self.harness.post("/api/scans", json={**body, "visitor_details": self.visitor_details()})
        self.assertEqual(response.json()["code"], "IDEMPOTENCY_KEY_REUSED")
        self.assertFalse(response.json()["approved"])
        self.assertEqual(self.harness.meals.meals, [])

    def test_master_read_after_commit_returns_original_meal_without_contact_details(self):
        body = self.read_body(token=self.harness.meals.master_token)
        first = self.harness.post("/api/scans", json={**body, "visitor_details": self.visitor_details()}).json()
        response = self.harness.post("/api/scan-app/read", json=body)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["approved"])
        self.assertTrue(response.json()["duplicate"])
        self.assertEqual(response.json()["meal_ids"], first["meal_ids"])
        self.assertNotIn("visitor@example.test", response.text)
        self.assertEqual(len(self.harness.meals.meals), 1)

    def test_master_read_cannot_recover_another_callers_committed_result(self):
        body = self.read_body(token=self.harness.meals.master_token)
        self.harness.post("/api/scans", json={**body, "visitor_details": self.visitor_details()})
        self.harness.login("second")
        response = self.harness.post("/api/scan-app/read", json=body)
        self.assertFalse(response.json()["approved"])
        self.assertEqual(response.json()["code"], "IDEMPOTENCY_KEY_REUSED")

    def test_master_retry_cannot_change_visitor_fields(self):
        body = self.read_body(token=self.harness.meals.master_token)
        body["visitor_details"] = self.visitor_details()
        self.harness.post("/api/scans", json=body)
        for key, value in (("company_name", "Changed Company"), ("name", "Different Person"),
                           ("email", "other@example.test"), ("phone", "+91 90000 00001")):
            with self.subTest(field=key):
                response = self.harness.post("/api/scans", json={
                    **body, "visitor_details": self.visitor_details(**{key: value}),
                })
                self.assertEqual(response.json()["code"], "IDEMPOTENCY_KEY_REUSED")
                self.assertFalse(response.json()["approved"])
        self.assertEqual(len(self.harness.meals.meals), 1)

    def test_visitor_details_are_trimmed_and_email_normalized(self):
        body = self.read_body(token=self.harness.meals.master_token)
        details = self.visitor_details(company_name="  Test Company  ", email="VISITOR@EXAMPLE.TEST")
        response = self.harness.post("/api/scans", json={**body, "visitor_details": details})
        self.assertEqual(response.status_code, 200)
        saved = self.harness.meals.scans[-1][1].visitor_details
        self.assertEqual(saved["company_name"], "Test Company")
        self.assertEqual(saved["email"], "visitor@example.test")

    def test_each_visitor_field_is_required(self):
        for field in self.visitor_details():
            with self.subTest(field=field):
                details = self.visitor_details()
                details.pop(field)
                response = self.harness.post("/api/scans", json={
                    **self.read_body(token=self.harness.meals.master_token), "visitor_details": details,
                })
                self.assert_error(response, 422, "INVALID_INPUT")
        self.assertEqual(self.harness.meals.meals, [])

    def test_invalid_visitor_values_do_not_reach_recording_service(self):
        for changes in ({"company_name": " "}, {"name": "A" * 151}, {"email": "not-an-email"},
                        {"phone": "123"}, {"phone": "not-a-phone"}, {"phone": "+1234567890123456"},
                        {"name": "Example\nPerson"}, {"staff_id": 1}):
            with self.subTest(changes=changes):
                response = self.harness.post("/api/scans", json={
                    **self.read_body(token=self.harness.meals.master_token),
                    "visitor_details": self.visitor_details(**changes),
                })
                self.assert_error(response, 422, "INVALID_INPUT")
        self.assertEqual(self.harness.meals.scans, [])

    def test_direct_visitor_requires_one_meal_and_no_legacy_authorization(self):
        for changes in ({"quantity": 2}, {"authorization_id": 501}):
            with self.subTest(changes=changes):
                response = self.harness.post("/api/scans", json={
                    **self.read_body(token=self.harness.meals.master_token),
                    "visitor_details": self.visitor_details(), **changes,
                })
                self.assert_error(response, 422, "INVALID_INPUT")
        self.assertEqual(self.harness.meals.scans, [])

    def test_employee_cannot_submit_visitor_form(self):
        response = self.harness.post("/api/scans", json={
            **self.read_body(), "visitor_details": self.visitor_details(),
        })
        self.assertFalse(response.json()["approved"])
        self.assertEqual(response.json()["code"], "VISITOR_DETAILS_NOT_APPLICABLE")
        self.assertEqual(self.harness.meals.meals, [])

    def test_read_does_not_accept_staff_category_or_visitor_overrides(self):
        for changes in ({"staff_id": 1}, {"waiter_id": 1}, {"role": "ADMIN"}, {"kind": "MASTER"},
                        {"quantity": 2}, {"authorization_id": 501}, {"visitor_details": self.visitor_details()}):
            with self.subTest(changes=changes):
                response = self.harness.post("/api/scan-app/read", json=self.read_body(**changes))
                self.assert_error(response, 422, "INVALID_INPUT")
        self.assertEqual(self.harness.meals.reads, [])

    def test_read_rejects_unauthenticated_users(self):
        self.harness.post("/api/auth/logout")
        response = self.harness.post("/api/scan-app/read", json=self.read_body())
        self.assert_error(response, 401, "AUTHENTICATION_REQUIRED")
        self.assertEqual(self.harness.meals.reads, [])

    def test_read_rejects_auditor_role(self):
        self.harness.login("auditor")
        response = self.harness.post("/api/scan-app/read", json=self.read_body())
        self.assert_error(response, 403, "ROLE_REQUIRED")
        self.assertEqual(self.harness.meals.reads, [])

    def test_admin_can_use_the_same_scan_only_endpoint(self):
        self.harness.login("admin")
        response = self.harness.post("/api/scan-app/read", json=self.read_body())
        self.assertTrue(response.json()["approved"])
        self.assertEqual(self.harness.meals.reads[-1][0], 1)

    def test_scan_only_login_cannot_access_administration_or_reports(self):
        for path in ("/api/employees", "/api/master-qrs", "/api/email-queue", "/api/staff",
                     "/api/reports/meals", "/api/reports/totals", "/api/reports/scans"):
            with self.subTest(path=path):
                self.assert_error(self.client.get(path), 403, "ROLE_REQUIRED")

    def test_read_requires_csrf(self):
        response = self.client.post("/api/scan-app/read", json=self.read_body(), headers={
            "Origin": self.harness.runtime.app_origin,
        })
        self.assert_error(response, 403, "CSRF_REJECTED")
        self.assertEqual(self.harness.meals.reads, [])

    def test_invalid_read_records_rejection_without_echoing_token(self):
        token = secrets.token_urlsafe(32)
        response = self.harness.post("/api/scan-app/read", json=self.read_body(token=token, request_id="invalid"))
        self.assert_error(response, 422, "INVALID_INPUT")
        self.assertEqual(self.harness.meals.invalid_scans, [(2, "EAST-01")])
        self.assertNotIn(token, response.text)

    def test_read_database_failure_never_returns_approval_or_sql(self):
        self.harness.meals.failure = RuntimeError("DB_PASSWORD=private INSERT INTO servings")
        response = self.harness.post("/api/scan-app/read", json=self.read_body())
        self.assert_error(response, 503, "SERVICE_UNAVAILABLE")
        self.assertNotIn("private", response.text)
        self.assertNotIn("INSERT", response.text)
        self.assertNotIn("approved", response.text)

    def test_read_unconfirmed_result_is_retryable(self):
        for code in ("SCAN_RECEIPT_UNCONFIRMED", "PROCESSING_UNCONFIRMED"):
            with self.subTest(code=code):
                self.harness.meals.read = Mock(return_value=ScanResult(False, code, str(uuid4())))
                response = self.harness.post("/api/scan-app/read", json=self.read_body())
                self.assert_error(response, 503, code)

    def test_read_revocation_expiry_and_inactive_rejections_pass_through(self):
        for code in ("QR_REVOKED", "QR_EXPIRED", "EMPLOYEE_INACTIVE"):
            with self.subTest(code=code):
                body = self.read_body()
                self.harness.meals.read = Mock(return_value=ScanResult(False, code, body["request_id"]))
                response = self.harness.post("/api/scan-app/read", json=body)
                self.assertEqual(response.status_code, 200)
                self.assertFalse(response.json()["approved"])
                self.assertEqual(response.json()["code"], code)


class ScanAppEntryTests(ApiTestCase):
    def test_dashboard_and_scanner_use_distinct_html_and_assets_without_database_access(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            frontend = root / "frontend" / "dist"
            admin_folder = frontend / "admin"
            scanner_folder = frontend / "scanner"
            admin_folder.mkdir(parents=True)
            scanner_folder.mkdir(parents=True)
            (admin_folder / "index.html").write_text("<html><title>Admin dashboard</title></html>")
            (admin_folder / "admin.js").write_text("const application = 'admin';")
            (scanner_folder / "index.html").write_text("<html><title>Meal scanner</title></html>")
            (scanner_folder / "scanner.js").write_text("const application = 'scanner';")
            with patch("meal_management.api.Path") as path:
                path.return_value.resolve.return_value.parents = {2: root}
                admin = ApiHarness()
            with patch("meal_management.scanner_api.Path") as path:
                path.return_value.resolve.return_value.parents = {2: root}
                scanner = ApiHarness(application="scanner")
            try:
                for harness, title, own_asset, foreign_asset in (
                    (admin, "Admin dashboard", "admin.js", "scanner.js"),
                    (scanner, "Meal scanner", "scanner.js", "admin.js"),
                ):
                    with self.subTest(application=title):
                        response = harness.client.get("/")
                        self.assertEqual(response.status_code, 200)
                        self.assertIn(title, response.text)
                        self.assertEqual(response.headers["cache-control"], "no-store")
                        self.assertIn("camera=(self)", response.headers["permissions-policy"])
                        self.assertEqual(harness.client.get("/assets/" + own_asset).status_code, 200)
                        self.assertEqual(harness.client.get("/assets/" + foreign_asset).status_code, 404)
                        self.assertEqual(harness.client.get("/scan").status_code, 404)
                        harness.services.database.transaction.assert_not_called()
            finally:
                admin.client.close()
                scanner.client.close()

    def test_missing_scanner_build_returns_safe_configuration_error(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch("meal_management.scanner_api.Path") as path:
                path.return_value.resolve.return_value.parents = {2: Path(folder)}
                harness = ApiHarness(application="scanner")
            try:
                self.assert_error(harness.client.get("/"), 400, "FRONTEND_BUILD_REQUIRED")
                harness.services.database.transaction.assert_not_called()
            finally:
                harness.client.close()
