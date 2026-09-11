import secrets
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from fastapi.testclient import TestClient

from meal_management.api import create_app
from meal_management.scanner_api import create_app as create_scanner_app
from meal_management.email_queue import EmailPreview
from meal_management.employees import Registration
from meal_management.errors import DomainError
from meal_management.models import ScanResult, ServerContext
from meal_management.qr import IssuedQr
from meal_management.reports import _date_range
from meal_management.runtime import RuntimeSettings
from meal_management.storage import StoredPhoto


class FakeStaff:
    session_hours = 8

    def __init__(self):
        self.password = secrets.token_urlsafe(32)
        self.sessions = {}
        self.users = {
            "admin@example.com": {"staff_id": 1, "display_name": "Admin", "roles": ["ADMIN"]},
            "waiter@example.com": {"staff_id": 2, "display_name": "Waiter", "roles": ["WAITER"]},
            "second@example.com": {"staff_id": 3, "display_name": "Second waiter", "roles": ["WAITER"]},
            "auditor@example.com": {"staff_id": 4, "display_name": "Auditor", "roles": ["AUDITOR"]},
        }
        self.authentication_calls = []
        self.create_staff = Mock(return_value=9)
        self.set_active = Mock()

    def authenticate(self, email, password):
        self.authentication_calls.append((email, password))
        user = self.users.get(email)
        if user is None or password != self.password:
            raise DomainError("INVALID_CREDENTIALS")
        token = secrets.token_urlsafe(32)
        self.sessions[token] = {**user, "email": email}
        return ServerContext(token)

    def current(self, context):
        identity = self.sessions.get(context.session_token)
        if identity is None:
            raise DomainError("AUTHENTICATION_REQUIRED")
        return dict(identity)

    def logout(self, context):
        self.sessions.pop(context.session_token, None)


class FakeMealStore:
    def __init__(self, staff):
        self.staff = staff
        self.employee_token = secrets.token_urlsafe(32)
        self.master_token = secrets.token_urlsafe(32)
        self.lock = threading.Lock()
        self.results = {}
        self.meals = []
        self.scans = []
        self.invalid_scans = []
        self.failure = None
        self.reads = []
        self.prepared = {}

    def _waiter(self, context):
        identity = self.staff.current(context)
        if not {"ADMIN", "WAITER"}.intersection(identity["roles"]):
            raise DomainError("ROLE_REQUIRED")
        return identity["staff_id"]

    def record_invalid(self, context, scanner_code=None):
        staff_id = self._waiter(context)
        if self.failure:
            raise self.failure
        self.invalid_scans.append((staff_id, scanner_code))

    def record(self, context, scan):
        staff_id = self._waiter(context)
        identity = str(scan.request_id)
        details = (staff_id, scan.token, scan.meal_type_id, scan.scanner_code, scan.quantity, scan.authorization_id, scan.visitor_details)
        with self.lock:
            self.scans.append((staff_id, scan))
            if self.failure:
                raise self.failure
            prepared = self.prepared.get(identity)
            if prepared is not None and prepared != details[:4]:
                return ScanResult(False, "IDEMPOTENCY_KEY_REUSED", identity)
            previous = self.results.get(identity)
            if previous is not None:
                original_details, result = previous
                if original_details != details:
                    return ScanResult(False, "IDEMPOTENCY_KEY_REUSED", identity)
                return replace(result, duplicate=True)
            code = None
            if scan.token == self.employee_token:
                if scan.quantity != 1:
                    code = "EMPLOYEE_QUANTITY_MUST_BE_ONE"
                elif scan.authorization_id is not None:
                    code = "EMPLOYEE_AUTHORIZATION_NOT_ALLOWED"
                elif scan.visitor_details is not None:
                    code = "VISITOR_DETAILS_NOT_APPLICABLE"
            elif scan.token == self.master_token:
                if scan.authorization_id != 501 and scan.visitor_details is None:
                    code = "VISITOR_DETAILS_REQUIRED"
            else:
                code = "QR_NOT_FOUND"
            if code:
                result = ScanResult(False, code, identity)
            else:
                start = len(self.meals) + 1
                meal_ids = tuple(range(start, start + scan.quantity))
                serving_id = len(self.results) + 1
                self.meals.extend(meal_ids)
                result = ScanResult(True, "APPROVED", identity, serving_id, meal_ids)
            self.results[identity] = (details, result)
            return result

    def read(self, context, scan):
        staff_id = self._waiter(context)
        self.reads.append((staff_id, scan))
        if self.failure:
            raise self.failure
        if scan.token != self.master_token:
            return self.record(context, scan)
        identifier = str(scan.request_id)
        binding = (staff_id, scan.token, scan.meal_type_id, scan.scanner_code)
        if identifier in self.results:
            if self.results[identifier][0][:4] != binding:
                return ScanResult(False, "IDEMPOTENCY_KEY_REUSED", identifier)
            return replace(self.results[identifier][1], duplicate=True)
        previous = self.prepared.setdefault(identifier, binding)
        if previous != binding:
            return ScanResult(False, "IDEMPOTENCY_KEY_REUSED", identifier)
        return {"kind": "MASTER", "next": "VISITOR_DETAILS", "request_id": identifier}


class ApiHarness:
    def __init__(self, runtime=None, application="admin"):
        self.runtime = runtime or RuntimeSettings(
            csrf_secret=secrets.token_bytes(32),
            login_rate_secret=secrets.token_bytes(32),
            app_origin="http://testserver",
            scanner_origin="http://testserver:8001",
            allowed_hosts=("testserver",),
        )
        self.staff = FakeStaff()
        self.meals = FakeMealStore(self.staff)
        self.meals.void = Mock()
        self.queries = Mock()
        self.queries.list_employees.return_value = {"items": [], "next_cursor": None}
        self.queries.employee.return_value = {
            "id": 7, "employee_code": "EMP-007", "full_name": "Asha Rao",
            "email": "asha@example.com", "department_id": 1,
            "is_active": True, "selfie_object_key": "a" * 64 + ".png",
        }
        self.queries.current_employee_qr.return_value = {"id": 101, "kind": "EMPLOYEE", "employee_id": 7}
        self.queries.qr_metadata.side_effect = lambda ctx, qr_id: {
            "id": qr_id, "kind": "MASTER" if qr_id >= 200 else "EMPLOYEE",
            "employee_id": None if qr_id >= 200 else 7,
        }
        self.queries.master_qrs.return_value = {"items": [{"id": 201, "kind": "MASTER"}], "next_cursor": None}
        self.queries.staff.return_value = {"items": [], "next_cursor": None}
        self.queries.email_status.return_value = {"items": [{"id": 1, "status": "QUEUED"}], "next_cursor": None}
        self.queries.visitor_authorizations.return_value = {"items": [], "next_cursor": None}
        self.queries.catalog.return_value = {"departments": [], "meal_types": [], "locations": [], "scanners": [], "waiters": []}
        self.queries.meal_history.side_effect = self._history
        self.queries.scans.side_effect = self._history
        self.storage = Mock()
        self.storage.put.return_value = StoredPhoto("b" * 64 + ".png", "image/png", 4)
        self.storage.read.return_value = (b"fake-private-photo", "image/png")
        self.limiter = Mock()
        self.services = SimpleNamespace(
            staff=self.staff,
            meals=self.meals,
            database=Mock(settings=SimpleNamespace(db_ssl_ca="configured-ca.pem")),
            employees=Mock(),
            qr=Mock(),
            approvals=Mock(),
            reports=Mock(),
            catalog=Mock(),
            email_queue=Mock(),
        )
        self.services.employees.register.return_value = Registration(7, 101, 1)
        self.services.qr.export_svg.return_value = '<svg xmlns="http://www.w3.org/2000/svg"></svg>'
        self.services.qr.issue_employee.return_value = IssuedQr(101, self.meals.employee_token)
        self.services.qr.replace_employee.return_value = IssuedQr(102, self.meals.employee_token)
        self.services.qr.issue_master.return_value = IssuedQr(201, self.meals.master_token)
        self.services.qr.replace_master.return_value = IssuedQr(202, self.meals.master_token)
        self.services.qr.retrieve.return_value = IssuedQr(201, self.meals.master_token)
        self.services.qr.resend_employee.return_value = 2
        self.services.email_queue.status.return_value = {"email_id": 2, "status": "DRAFT", "delivery_mode": "SINGLE"}
        self.services.approvals.authorize.return_value = 501
        self.services.reports.meal_report.side_effect = self._totals
        self.services.email_queue.preview.return_value = EmailPreview(
            1, "asha@example.com", '<script>alert("name")</script>',
            self.meals.employee_token, '<svg xmlns="http://www.w3.org/2000/svg"></svg>',
        )
        if application == "scanner":
            self.app = create_scanner_app(self.services, self.runtime, limiter=self.limiter)
            self.runtime = self.app.state.runtime
        else:
            self.app = create_app(self.services, self.runtime, self.queries, self.storage, self.limiter)
        self.receipts = Mock()
        self.development_seed = Mock()
        self.development_seed.manifest.return_value = {"employees": [], "master": None, "waiters": []}
        self.development_seed.export_svg.return_value = '<svg xmlns="http://www.w3.org/2000/svg"></svg>'
        self.app.state.scan_receipts = self.receipts
        self.app.state.development_seed = self.development_seed
        self.client = TestClient(self.app, base_url=self.runtime.app_origin)

    def _history(self, context, start, end, **filters):
        _date_range(start, end)
        return {"items": [], "next_cursor": None}

    def _totals(self, context, start, end):
        _date_range(start, end)
        return {"totals": {"meal_count": 5, "serving_count": 4, "employee_meals": 3, "visitor_meals": 2}}

    def headers(self):
        response = self.client.get("/api/auth/csrf")
        return {"Origin": self.runtime.app_origin, "X-CSRF-Token": response.json()["csrf_token"]}

    def login(self, role="admin"):
        return self.client.post(
            "/api/auth/login",
            json={"email": role + "@example.com", "password": self.staff.password},
            headers=self.headers(),
        )

    def post(self, path, **kwargs):
        return self.client.post(path, headers=self.headers(), **kwargs)

    def patch(self, path, **kwargs):
        return self.client.patch(path, headers=self.headers(), **kwargs)

    def delete(self, path, **kwargs):
        return self.client.request("DELETE", path, headers=self.headers(), **kwargs)

    def scan_body(self, **changes):
        body = {
            "request_id": str(uuid4()), "token": self.meals.employee_token,
            "meal_type_id": 1, "scanner_code": "EAST-01", "quantity": 1,
        }
        body.update(changes)
        return body


class ApiTestCase(unittest.TestCase):
    def setUp(self):
        self.harness = ApiHarness()
        self.client = self.harness.client
        self.addCleanup(self.client.close)

    def assert_error(self, response, status, code):
        self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(response.json()["error"]["code"], code)
        self.assertRegex(response.json()["error"]["request_id"], r"^[0-9a-f]{32}$")


class AuthenticationApiTests(ApiTestCase):
    def test_login_uses_staff_service_and_sets_private_session_cookie(self):
        response = self.harness.login()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["roles"], ["ADMIN"])
        self.assertEqual(self.harness.staff.authentication_calls[-1], ("admin@example.com", self.harness.staff.password))
        cookies = response.headers.get_list("set-cookie")
        self.assertTrue(any("meal_session=" in value and "HttpOnly" in value and "SameSite=strict" in value for value in cookies))
        self.assertNotIn("session_token", response.text)
        self.assertNotIn(self.harness.staff.password, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_csrf_is_required_for_login(self):
        response = self.client.post(
            "/api/auth/login", json={"email": "admin@example.com", "password": self.harness.staff.password},
            headers={"Origin": self.harness.runtime.app_origin},
        )
        self.assert_error(response, 403, "CSRF_REJECTED")
        self.assertEqual(self.harness.staff.authentication_calls, [])

    def test_origin_is_required_and_must_match_exactly(self):
        for origin in (None, "https://attacker.example", "http://testserver.attacker.example"):
            with self.subTest(origin=origin):
                headers = self.harness.headers()
                if origin is None:
                    headers.pop("Origin")
                else:
                    headers["Origin"] = origin
                response = self.client.post("/api/auth/login", json={"email": "admin@example.com", "password": self.harness.staff.password}, headers=headers)
                self.assert_error(response, 403, "ORIGIN_REJECTED")

    def test_successful_login_rotates_session_and_csrf_binding(self):
        previous_csrf = self.harness.headers()["X-CSRF-Token"]
        first = self.harness.login()
        first_session = self.client.cookies.get("meal_session")
        self.assertNotEqual(first.json()["csrf_token"], previous_csrf)
        second = self.harness.login()
        self.assertNotEqual(first_session, self.client.cookies.get("meal_session"))
        self.assertNotEqual(first.json()["csrf_token"], second.json()["csrf_token"])

    def test_prelogin_csrf_cannot_authorize_authenticated_mutation(self):
        headers = self.harness.headers()
        self.harness.login()
        response = self.client.post("/api/employees/7/qr/resend", headers=headers)
        self.assert_error(response, 403, "CSRF_REJECTED")
        self.harness.services.qr.resend_employee.assert_not_called()

    def test_wrong_password_returns_safe_error_and_no_session(self):
        response = self.harness.post("/api/auth/login", json={"email": "admin@example.com", "password": secrets.token_urlsafe(16)})
        self.assert_error(response, 401, "INVALID_CREDENTIALS")
        self.assertIsNone(self.client.cookies.get("meal_session"))

    def test_authentication_transaction_failure_sets_no_session(self):
        self.harness.staff.authenticate = Mock(side_effect=RuntimeError("credential database password=hidden"))
        response = self.harness.login()
        self.assert_error(response, 503, "SERVICE_UNAVAILABLE")
        self.assertIsNone(self.client.cookies.get("meal_session"))
        self.assertNotIn("hidden", response.text)

    def test_logout_also_clears_an_expired_session_cookie(self):
        self.harness.login()
        self.harness.staff.sessions.clear()
        response = self.harness.post("/api/auth/logout")
        self.assertEqual(response.status_code, 204)
        self.assertIsNone(self.client.cookies.get("meal_session"))

    def test_login_rate_limit_stops_authentication_and_sets_retry_after(self):
        self.harness.limiter.consume.side_effect = DomainError("RATE_LIMITED")
        response = self.harness.login()
        self.assert_error(response, 429, "RATE_LIMITED")
        self.assertEqual(response.headers["retry-after"], "900")
        self.assertEqual(self.harness.staff.authentication_calls, [])

    def test_login_limiter_uses_peer_not_forged_forwarded_ip(self):
        response = self.client.post(
            "/api/auth/login", json={"email": "admin@example.com", "password": self.harness.staff.password},
            headers={**self.harness.headers(), "X-Forwarded-For": "192.0.2.19"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(self.harness.limiter.consume.call_args.args[1], "192.0.2.19")

    def test_logout_revokes_session_and_clears_cookies(self):
        self.harness.login()
        session = self.client.cookies.get("meal_session")
        response = self.harness.post("/api/auth/logout")
        self.assertEqual(response.status_code, 204)
        self.assertNotIn(session, self.harness.staff.sessions)
        self.assertIsNone(self.client.cookies.get("meal_session"))
        self.assertIsNone(self.client.cookies.get("meal_csrf_seed"))
        self.assert_error(self.client.get("/api/auth/me"), 401, "AUTHENTICATION_REQUIRED")

    def test_forged_session_does_not_authenticate(self):
        self.client.cookies.set("meal_session", secrets.token_urlsafe(32))
        self.assert_error(self.client.get("/api/auth/me"), 401, "AUTHENTICATION_REQUIRED")

    def test_submitted_role_and_staff_identity_are_rejected(self):
        response = self.harness.post(
            "/api/auth/login", json={"email": "waiter@example.com", "password": self.harness.staff.password, "role": "ADMIN", "staff_id": 1},
        )
        self.assert_error(response, 422, "INVALID_INPUT")
        self.assertEqual(self.harness.staff.authentication_calls, [])

    def test_no_public_registration_bootstrap_or_api_documentation(self):
        for path in ("/api/auth/register", "/api/bootstrap-admin", "/docs", "/openapi.json"):
            with self.subTest(path=path):
                self.assert_error(self.client.get(path), 404, "NOT_FOUND")

    def test_secure_development_https_uses_host_prefixed_cookie(self):
        runtime = replace(self.harness.runtime, app_origin="https://testserver", cookie_secure=True, session_cookie="__Host-meal_session")
        harness = ApiHarness(runtime)
        self.addCleanup(harness.client.close)
        response = harness.login()
        self.assertEqual(response.status_code, 200)
        cookie = next(value for value in response.headers.get_list("set-cookie") if value.startswith("__Host-meal_session="))
        self.assertIn("Secure", cookie)
        self.assertIn("Path=/", cookie)
        self.assertNotIn("Domain=", cookie)


class PermissionApiTests(ApiTestCase):
    def test_anonymous_cannot_read_administration(self):
        for path in ("/api/employees", "/api/employees/7/qr", "/api/master-qrs", "/api/email-queue"):
            with self.subTest(path=path):
                self.assert_error(self.client.get(path), 401, "AUTHENTICATION_REQUIRED")

    def test_waiter_cannot_read_employee_qrs_reports_or_emails(self):
        self.harness.login("waiter")
        paths = (
            "/api/employees", "/api/employees/7/qr", "/api/employees/8/qr",
            "/api/employees/7/photo", "/api/master-qrs", "/api/master-qrs/201",
            "/api/staff", "/api/email-queue", "/api/email-queue/1/preview",
            "/api/reports/meals?start=2026-09-01T00:00:00Z&end=2026-09-02T00:00:00Z",
            "/api/reports/totals?start=2026-09-01T00:00:00Z&end=2026-09-02T00:00:00Z",
            "/api/reports/scans?start=2026-09-01T00:00:00Z&end=2026-09-02T00:00:00Z",
        )
        for path in paths:
            with self.subTest(path=path):
                self.assert_error(self.client.get(path), 403, "ROLE_REQUIRED")
        self.harness.services.qr.export_svg.assert_not_called()
        self.harness.services.email_queue.preview.assert_not_called()

    def test_waiter_cannot_register_staff_or_employees(self):
        self.harness.login("waiter")
        for path, body in (
            ("/api/employees", {"employee_code": "EMP-9", "full_name": "New employee", "email": "new@example.com", "department_id": 1}),
            ("/api/staff", {"display_name": "New admin", "email": "new@example.com", "password": secrets.token_urlsafe(32), "roles": ["ADMIN"]}),
        ):
            with self.subTest(path=path):
                self.assert_error(self.harness.post(path, json=body), 403, "ROLE_REQUIRED")
        self.harness.staff.create_staff.assert_not_called()
        self.harness.services.employees.register.assert_not_called()

    def test_admin_can_scan_without_a_second_role(self):
        self.harness.login()
        response = self.harness.post("/api/scans", json=self.harness.scan_body())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["approved"])
        self.assertEqual(self.harness.meals.scans[0][0], 1)

    def test_admin_malformed_scan_is_rejected_and_logged(self):
        self.harness.login()
        response = self.harness.post("/api/scans", json={"request_id": "invalid"})
        self.assert_error(response, 422, "INVALID_INPUT")
        self.assertEqual(self.harness.meals.invalid_scans, [(1, None)])

    def test_auditor_cannot_access_waiter_or_admin_routes(self):
        self.harness.login("auditor")
        self.assert_error(self.client.get("/api/employees"), 403, "ROLE_REQUIRED")
        self.assert_error(self.harness.post("/api/scans", json=self.harness.scan_body()), 403, "ROLE_REQUIRED")

    def test_waiter_can_access_catalog_and_own_authorization_query(self):
        self.harness.login("waiter")
        self.assertEqual(self.client.get("/api/catalog").status_code, 200)
        self.assertEqual(self.client.get("/api/visitor-authorizations").status_code, 200)
        context = self.harness.queries.visitor_authorizations.call_args.args[0]
        self.assertEqual(self.harness.staff.current(context)["staff_id"], 2)


class EmployeeAdminApiTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.harness.login()

    def test_registration_reuses_service_and_returns_identifiers_only(self):
        body = {"employee_code": "EMP-007", "full_name": "Asha Rao", "email": "asha@example.com", "department_id": 1}
        response = self.harness.post("/api/employees", json=body)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json(), {"employee_id": 7, "qr_id": 101, "email_id": 1})
        context = self.harness.services.employees.register.call_args.args[0]
        self.assertEqual(self.harness.staff.current(context)["staff_id"], 1)
        self.assertNotIn("token", response.text)

    def test_employee_update_preserves_omitted_fields(self):
        response = self.client.patch("/api/employees/7", json={"full_name": "Updated name"}, headers=self.harness.headers())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.harness.services.employees.update.call_args.kwargs, {"full_name": "Updated name"})

    def test_employee_deactivation_uses_status_endpoint(self):
        response = self.harness.patch("/api/employees/7/active", json={"is_active": False})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["is_active"])
        self.assertEqual(self.harness.services.employees.set_active.call_args.args[1:], (7, False))

    def test_employee_removal_requires_reason_and_uses_authenticated_service(self):
        self.assert_error(self.harness.delete("/api/employees/7", json={"reason": ""}), 422, "INVALID_INPUT")
        response = self.harness.delete("/api/employees/7", json={"reason": "Duplicate employee record"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"employee_id": 7, "removed": True})
        context, employee_id, reason = self.harness.services.employees.archive.call_args.args
        self.assertEqual(self.harness.staff.current(context)["staff_id"], 1)
        self.assertEqual((employee_id, reason), (7, "Duplicate employee record"))

    def test_meal_removal_requires_admin_and_reason(self):
        self.assert_error(self.harness.delete("/api/meals/12", json={"reason": ""}), 422, "INVALID_INPUT")
        response = self.harness.delete("/api/meals/12", json={"reason": "Recorded by mistake"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"meal_id": 12, "removed": True})
        context, meal_id, reason = self.harness.services.meals.void.call_args.args
        self.assertEqual(self.harness.staff.current(context)["staff_id"], 1)
        self.assertEqual((meal_id, reason), (12, "Recorded by mistake"))

    def test_waiter_cannot_remove_employees_or_meals(self):
        self.harness.login("waiter")
        for path in ("/api/employees/7", "/api/meals/12"):
            with self.subTest(path=path):
                self.assert_error(self.harness.delete(path, json={"reason": "Unauthorized change"}), 403, "ROLE_REQUIRED")
        self.harness.services.employees.archive.assert_not_called()
        self.harness.services.meals.void.assert_not_called()

    def test_employee_qr_is_private_and_scoped_to_employee_lookup(self):
        response = self.client.get("/api/employees/7/qr")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["kind"], "EMPLOYEE")
        self.assertEqual(self.harness.queries.current_employee_qr.call_args.args[1], 7)
        self.assertEqual(self.harness.services.qr.export_svg.call_args.args[1], 101)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")

    def test_qr_resend_prepares_same_credential_draft_without_sending(self):
        response = self.harness.post("/api/employees/7/qr/resend")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json(), {"email_id": 2, "status": "DRAFT", "delivery_mode": "SINGLE"})
        self.assertEqual(self.harness.services.qr.resend_employee.call_args.args[1], 7)
        self.harness.services.qr.issue_master.assert_not_called()

    def test_qr_replacement_returns_no_raw_token(self):
        response = self.harness.post("/api/employees/7/qr/replace", json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["qr_id"], 102)
        self.assertNotIn(self.harness.meals.employee_token, response.text)

    def test_employee_qr_issue_uses_atomic_issue_service(self):
        self.harness.queries.current_employee_qr.return_value = None
        response = self.harness.post("/api/employees/7/qr", json={})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json(), {"employee_id": 7, "qr_id": 101})
        self.assertEqual(self.harness.services.qr.issue_employee.call_args.args[1:], (7, None))
        self.harness.services.qr.replace_employee.assert_not_called()

    def test_missing_qr_returns_not_found(self):
        self.harness.queries.current_employee_qr.return_value = None
        self.assert_error(self.client.get("/api/employees/7/qr"), 404, "QR_NOT_FOUND")

    def test_qr_revoke_passes_reason_and_resolved_credential(self):
        response = self.harness.post("/api/employees/7/qr/revoke", json={"reason": "Lost credential"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.harness.services.qr.revoke.call_args.args[1:], (101, "Lost credential"))

    def test_private_photo_uses_storage_reference_and_authenticated_route(self):
        response = self.client.get("/api/employees/7/photo")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"fake-private-photo")
        self.assertEqual(response.headers["content-type"], "image/png")
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_photo_upload_rolls_back_new_file_when_service_rejects_update(self):
        self.harness.services.employees.update.side_effect = DomainError("EMPLOYEE_NOT_FOUND")
        response = self.client.post(
            "/api/employees/7/photo", content=b"fake", headers={**self.harness.headers(), "Content-Type": "image/png"},
        )
        self.assert_error(response, 404, "EMPLOYEE_NOT_FOUND")
        self.harness.storage.delete.assert_called_once_with("b" * 64 + ".png")

    def test_email_preview_is_local_private_and_escaped(self):
        response = self.client.get("/api/email-queue/1/preview")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn("&lt;script&gt;", response.text)
        self.assertNotIn("<script>", response.text)
        self.assertNotIn(self.harness.meals.employee_token, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")


class EmailSettingsApiTests(ApiTestCase):
    def test_email_settings_require_authenticated_admin_access(self):
        self.assert_error(self.client.get("/api/email-settings"), 401, "AUTHENTICATION_REQUIRED")
        for role in ("waiter", "auditor"):
            with self.subTest(role=role):
                self.harness.login(role)
                self.assert_error(self.client.get("/api/email-settings"), 403, "ROLE_REQUIRED")

    def test_preview_settings_expose_only_safe_mode_flags(self):
        self.harness.login()
        response = self.client.get("/api/email-settings")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "backend": "preview", "sending_enabled": False, "preview_available": True,
            "automatic_enabled": False, "worker_running": False, "poll_seconds": 5, "batch_size": 10,
            "approval_required": True, "process_batch_size": 10,
        })
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.harness.services.database.transaction.assert_not_called()
        self.harness.services.email_queue.preview.assert_not_called()

    def test_provider_modes_expose_enabled_flag_without_sender_or_credentials(self):
        for backend in ("gmail", "ses"):
            for enabled in (False, True):
                with self.subTest(backend=backend, sending_enabled=enabled):
                    runtime = replace(self.harness.runtime, email_backend=backend, email_send_enabled=enabled, email_sender="private-sender@example.test")
                    harness = ApiHarness(runtime)
                    self.addCleanup(harness.client.close)
                    harness.login()
                    response = harness.client.get("/api/email-settings")
                    self.assertEqual(response.json(), {
                        "backend": backend, "sending_enabled": enabled, "preview_available": False,
                        "automatic_enabled": False, "worker_running": False, "poll_seconds": 5, "batch_size": 10,
                        "approval_required": True, "process_batch_size": 10,
                    })
                    self.assertNotIn("private-sender", response.text)
                    harness.services.database.transaction.assert_not_called()
                    self.assert_error(harness.client.get("/api/email-queue/1/preview"), 400, "EMAIL_PREVIEW_DISABLED")
                    harness.services.email_queue.preview.assert_not_called()

    def test_runtime_limits_are_exposed_without_database_access(self):
        runtime = replace(self.harness.runtime, max_photo_bytes=4000000, email_process_limit=1)
        harness = ApiHarness(runtime)
        self.addCleanup(harness.client.close)
        configuration = harness.client.get("/api/application")
        self.assertEqual(configuration.json(), {"scanner_url": runtime.scanner_origin, "max_photo_bytes": 4000000})
        harness.login()
        settings = harness.client.get("/api/email-settings")
        self.assertEqual(settings.status_code, 200)
        self.assertEqual(settings.json()["process_batch_size"], 1)
        self.assertEqual(settings.json()["batch_size"], 10)
        harness.services.database.transaction.assert_not_called()

    def test_email_settings_are_not_exposed_on_scanner_application(self):
        harness = ApiHarness(application="scanner")
        self.addCleanup(harness.client.close)
        self.assert_error(harness.client.get("/api/email-settings"), 404, "NOT_FOUND")
        harness.services.database.transaction.assert_not_called()

    def test_email_configuration_and_unscoped_sending_remain_unavailable(self):
        self.harness.login()
        self.assertEqual(self.harness.post("/api/email-settings", json={}).status_code, 405)
        self.assert_error(self.harness.post("/api/email-queue/1/send", json={}), 404, "NOT_FOUND")
        self.assertEqual(self.harness.post("/api/email-queue/send", json={}).status_code, 405)


class MasterAuthorizationApiTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.harness.login()

    def authorization_body(self):
        return {"request_id": str(uuid4()), "master_qr_id": 201, "waiter_id": 2, "scanner_code": "EAST-01", "meal_type_id": 1, "quantity": 3, "visitor_name": "Ravi Patel"}

    def test_master_creation_returns_master_category(self):
        response = self.harness.post("/api/master-qrs", json={})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["kind"], "MASTER")
        self.assertNotIn(self.harness.meals.master_token, response.text)

    def test_employee_qr_cannot_be_retrieved_through_master_route(self):
        self.assert_error(self.client.get("/api/master-qrs/101"), 400, "MASTER_QR_REQUIRED")
        self.harness.services.qr.export_svg.assert_not_called()

    def test_authorization_uses_authenticated_admin_and_server_retrieved_master(self):
        body = self.authorization_body()
        response = self.harness.post("/api/visitor-authorizations", json=body)
        self.assertEqual(response.status_code, 201)
        args = self.harness.services.approvals.authorize.call_args
        self.assertEqual(self.harness.staff.current(args.args[0])["staff_id"], 1)
        self.assertEqual(args.kwargs["master_token"], self.harness.meals.master_token)
        self.assertEqual(args.kwargs["waiter_id"], 2)
        self.assertEqual(args.kwargs["quantity"], 3)
        self.assertEqual(response.json()["request_id"], body["request_id"])

    def test_cannot_forge_authorizing_admin_or_supply_third_qr_kind(self):
        for change in ({"authorized_by": 99}, {"role": "ADMIN"}, {"kind": "GUEST"}):
            with self.subTest(change=change):
                response = self.harness.post("/api/visitor-authorizations", json={**self.authorization_body(), **change})
                self.assert_error(response, 422, "INVALID_INPUT")
        self.harness.services.approvals.authorize.assert_not_called()

    def test_waiter_cannot_authorize_visitor_serving(self):
        self.harness.login("waiter")
        self.assert_error(self.harness.post("/api/visitor-authorizations", json=self.authorization_body()), 403, "ROLE_REQUIRED")


class ScanApiTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.harness.login("waiter")

    def test_employee_scan_passes_authenticated_context(self):
        response = self.harness.post("/api/scans", json=self.harness.scan_body())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["approved"])
        self.assertEqual(len(response.json()["meal_ids"]), 1)
        self.assertEqual(self.harness.meals.scans[0][0], 2)
        self.assertNotIn(self.harness.meals.employee_token, response.text)

    def test_same_request_returns_original_committed_meals(self):
        body = self.harness.scan_body()
        first = self.harness.post("/api/scans", json=body).json()
        second = self.harness.post("/api/scans", json=body).json()
        self.assertTrue(second["approved"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["serving_id"], second["serving_id"])
        self.assertEqual(first["meal_ids"], second["meal_ids"])
        self.assertEqual(len(self.harness.meals.meals), 1)

    def test_new_request_identifier_records_another_employee_meal(self):
        first = self.harness.post("/api/scans", json=self.harness.scan_body()).json()
        second = self.harness.post("/api/scans", json=self.harness.scan_body()).json()
        self.assertNotEqual(first["meal_ids"], second["meal_ids"])
        self.assertEqual(len(self.harness.meals.meals), 2)

    def test_retry_cannot_change_request_details(self):
        body = self.harness.scan_body()
        self.harness.post("/api/scans", json=body)
        response = self.harness.post("/api/scans", json={**body, "meal_type_id": 2})
        self.assertFalse(response.json()["approved"])
        self.assertEqual(response.json()["code"], "IDEMPOTENCY_KEY_REUSED")
        self.assertEqual(len(self.harness.meals.meals), 1)

    def test_retry_cannot_change_authenticated_caller(self):
        body = self.harness.scan_body()
        self.harness.post("/api/scans", json=body)
        self.harness.login("second")
        response = self.harness.post("/api/scans", json=body)
        self.assertFalse(response.json()["approved"])
        self.assertEqual(response.json()["code"], "IDEMPOTENCY_KEY_REUSED")
        self.assertEqual(len(self.harness.meals.meals), 1)

    def test_master_scan_records_authorized_quantity(self):
        body = self.harness.scan_body(token=self.harness.meals.master_token, authorization_id=501, quantity=3)
        response = self.harness.post("/api/scans", json=body)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["approved"])
        self.assertEqual(len(response.json()["meal_ids"]), 3)

    def test_master_without_details_or_legacy_authorization_returns_clear_rejection(self):
        response = self.harness.post("/api/scans", json=self.harness.scan_body(token=self.harness.meals.master_token))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["approved"])
        self.assertEqual(response.json()["code"], "VISITOR_DETAILS_REQUIRED")
        self.assertEqual(self.harness.meals.meals, [])

    def test_original_rejection_is_returned_on_retry(self):
        body = self.harness.scan_body(token=secrets.token_urlsafe(32))
        first = self.harness.post("/api/scans", json=body).json()
        second = self.harness.post("/api/scans", json=body).json()
        self.assertEqual(first["code"], second["code"])
        self.assertFalse(second["approved"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(self.harness.meals.meals, [])

    def test_simultaneous_http_retries_use_one_result_from_fake_locked_store(self):
        body = self.harness.scan_body()
        headers = self.harness.headers()
        cookies = dict(self.client.cookies)
        barrier = threading.Barrier(2)

        def submit():
            with TestClient(self.harness.app, base_url=self.harness.runtime.app_origin) as client:
                client.cookies.update(cookies)
                barrier.wait(timeout=5)
                return client.post("/api/scans", json=body, headers=headers)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda value: submit(), range(2)))
        self.assertTrue(all(response.status_code == 200 for response in results))
        self.assertTrue(all(response.json()["approved"] for response in results))
        self.assertEqual(results[0].json()["meal_ids"], results[1].json()["meal_ids"])
        self.assertEqual(len(self.harness.meals.meals), 1)

    def test_invalid_input_records_rejection_without_echoing_secrets(self):
        token = secrets.token_urlsafe(32)
        response = self.harness.post("/api/scans", json=self.harness.scan_body(request_id="not-a-uuid", token=token))
        self.assert_error(response, 422, "INVALID_INPUT")
        self.assertEqual(self.harness.meals.invalid_scans, [(2, "EAST-01")])
        self.assertNotIn(token, response.text)
        self.assertEqual(self.harness.meals.meals, [])

    def test_malformed_json_is_safely_logged_without_echoing_payload(self):
        token = secrets.token_urlsafe(32)
        response = self.client.post(
            "/api/scans", content='{"token":"' + token,
            headers={**self.harness.headers(), "Content-Type": "application/json"},
        )
        self.assert_error(response, 422, "INVALID_INPUT")
        self.assertEqual(self.harness.meals.invalid_scans, [(2, None)])
        self.assertNotIn(token, response.text)

    def test_strict_scan_integer_inputs_cannot_coerce_strings_or_booleans(self):
        for changes in ({"quantity": True}, {"quantity": "1"}, {"quantity": 0}, {"quantity": 65536}, {"meal_type_id": True}, {"authorization_id": "501"}):
            with self.subTest(changes=changes):
                response = self.harness.post("/api/scans", json=self.harness.scan_body(**changes))
                self.assert_error(response, 422, "INVALID_INPUT")
        self.assertEqual(self.harness.meals.meals, [])

    def test_submitted_staff_id_or_role_cannot_override_authentication(self):
        for extra in ({"staff_id": 1}, {"waiter_id": 1}, {"roles": ["ADMIN"]}, {"kind": "GUEST"}):
            with self.subTest(extra=extra):
                self.assert_error(self.harness.post("/api/scans", json=self.harness.scan_body(**extra)), 422, "INVALID_INPUT")
        self.assertEqual(self.harness.meals.meals, [])

    def test_database_commit_failure_does_not_return_approval_or_details(self):
        self.harness.meals.failure = RuntimeError("database password=hidden SQL INSERT INTO meals")
        response = self.harness.post("/api/scans", json=self.harness.scan_body())
        self.assert_error(response, 503, "SERVICE_UNAVAILABLE")
        self.assertNotIn("hidden", response.text)
        self.assertNotIn("INSERT", response.text)
        self.assertNotIn("approved", response.text)
        self.assertEqual(self.harness.meals.meals, [])

    def test_unconfirmed_commit_result_returns_retryable_error(self):
        for code in ("PROCESSING_UNCONFIRMED", "SCAN_RECEIPT_UNCONFIRMED"):
            with self.subTest(code=code):
                self.harness.meals.record = Mock(return_value=ScanResult(False, code, str(uuid4())))
                response = self.harness.post("/api/scans", json=self.harness.scan_body())
                self.assert_error(response, 503, code)

    def test_invalid_scan_logging_failure_is_not_reported_as_recorded(self):
        self.harness.meals.failure = RuntimeError("database secret")
        response = self.harness.post("/api/scans", json=self.harness.scan_body(quantity=0))
        self.assert_error(response, 503, "SERVICE_UNAVAILABLE")
        self.assertEqual(self.harness.meals.invalid_scans, [])


class ValidationAndReportApiTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.harness.login()

    def test_strict_employee_ids_reject_boolean_string_zero_and_overflow(self):
        for value in (True, "1", 0, -1, 2**64):
            with self.subTest(value=value):
                body = {"employee_code": "EMP-007", "full_name": "Asha", "email": "asha@example.com", "department_id": value}
                self.assert_error(self.harness.post("/api/employees", json=body), 422, "INVALID_INPUT")
        self.harness.services.employees.register.assert_not_called()

    def test_date_fields_reject_naive_expiry(self):
        self.assert_error(self.harness.post("/api/master-qrs", json={"expires_at": "2027-01-01T12:00:00"}), 422, "INVALID_INPUT")

    def test_required_fields_and_unknown_fields_are_rejected(self):
        for body in ({}, {"full_name": "Asha"}, {"employee_code": "E1", "full_name": "Asha", "email": "a@example.com", "department_id": 1, "is_admin": True}):
            with self.subTest(body=body):
                self.assert_error(self.harness.post("/api/employees", json=body), 422, "INVALID_INPUT")

    def test_pagination_values_are_validated(self):
        for query in ("limit=0", "limit=1001", "limit=-1", "after_id=-1", "limit=abc"):
            with self.subTest(query=query):
                self.assert_error(self.client.get("/api/employees?" + query), 422, "INVALID_INPUT")
        self.harness.queries.list_employees.assert_not_called()

    def test_employee_filters_and_cursor_are_forwarded(self):
        response = self.client.get("/api/employees", params={"limit": 20, "after_id": 7, "q": "Asha", "active": "false"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.harness.queries.list_employees.call_args.kwargs, {"limit": 20, "after_id": 7, "q": "Asha", "active": False})

    def test_report_date_filters_preserve_timezone_information(self):
        response = self.client.get("/api/reports/meals", params={
            "start": "2026-09-01T05:30:00+05:30", "end": "2026-09-02T05:30:00+05:30",
            "employee_id": 7, "limit": 10, "after_id": 12,
        })
        self.assertEqual(response.status_code, 200)
        args = self.harness.queries.meal_history.call_args
        self.assertIsNotNone(args.args[1].utcoffset())
        self.assertEqual(args.args[1].astimezone(timezone.utc), datetime(2026, 9, 1, tzinfo=timezone.utc))
        self.assertEqual(args.kwargs, {"limit": 10, "after_id": 12, "employee_id": 7})

    def test_naive_or_reversed_report_dates_are_rejected_by_service_validation(self):
        for start, end, code in (
            ("2026-09-01T00:00:00", "2026-09-02T00:00:00", "UTC_OFFSET_REQUIRED"),
            ("2026-09-03T00:00:00Z", "2026-09-02T00:00:00Z", "INVALID_DATE_RANGE"),
        ):
            with self.subTest(code=code):
                self.assert_error(self.client.get("/api/reports/meals", params={"start": start, "end": end}), 400, code)

    def test_invalid_report_date_syntax_is_rejected_before_query(self):
        response = self.client.get("/api/reports/meals", params={"start": "yesterday", "end": "tomorrow"})
        self.assert_error(response, 422, "INVALID_INPUT")
        self.harness.queries.meal_history.assert_not_called()

    def test_rejected_scan_filter_is_validated_and_forwarded(self):
        params = {"start": "2026-09-01T00:00:00Z", "end": "2026-09-02T00:00:00Z", "outcome": "REJECTED"}
        response = self.client.get("/api/reports/scans", params=params)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.harness.queries.scans.call_args.kwargs["outcome"], "REJECTED")
        self.assert_error(self.client.get("/api/reports/scans", params={**params, "outcome": "UNKNOWN"}), 422, "INVALID_INPUT")

    def test_totals_return_service_totals(self):
        response = self.client.get("/api/reports/totals", params={"start": "2026-09-01T00:00:00Z", "end": "2026-09-02T00:00:00Z"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["totals"]["meal_count"], 5)

    def test_email_status_is_paginated_and_filtered(self):
        response = self.client.get("/api/email-queue", params={"limit": 10, "after_id": 3, "employee_id": 7})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.harness.queries.email_status.call_args.kwargs, {"limit": 10, "after_id": 3, "employee_id": 7, "delivery_scope": "all"})
        self.assertNotIn("payload_ciphertext", response.text)

    def test_sensitive_errors_include_security_headers(self):
        self.harness.queries.employee.side_effect = RuntimeError("SELECT employee password=hidden")
        response = self.client.get("/api/employees/7")
        self.assert_error(response, 503, "SERVICE_UNAVAILABLE")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn("default-src 'self'", response.headers["content-security-policy"])
        self.assertNotIn("hidden", response.text)

    def test_oversized_json_is_rejected_before_employee_service(self):
        response = self.harness.post("/api/employees", content=b"x" * 65537)
        self.assert_error(response, 413, "REQUEST_TOO_LARGE")
        self.harness.services.employees.register.assert_not_called()

    def test_untrusted_host_is_rejected(self):
        response = self.client.get("/health/live", headers={"Host": "attacker.example"})
        self.assertEqual(response.status_code, 400)


class HealthApiTests(ApiTestCase):
    def test_path_identifiers_are_bounded_before_database_services(self):
        self.harness.login()
        for identifier in ("0", "-1", str(2**64)):
            with self.subTest(identifier=identifier):
                response = self.client.get("/api/employees/" + identifier)
                self.assert_error(response, 422, "INVALID_INPUT")
        self.harness.queries.employee.assert_not_called()

    def test_report_cursor_rejects_unsigned_bigint_overflow(self):
        self.harness.login()
        response = self.client.get("/api/employees", params={"after_id": str(2**64)})
        self.assert_error(response, 422, "INVALID_INPUT")
        self.harness.queries.list_employees.assert_not_called()

    def test_liveness_does_not_connect_to_database(self):
        response = self.client.get("/health/live")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        self.harness.services.database.transaction.assert_not_called()

    def test_readiness_failure_is_safe(self):
        self.harness.services.database.transaction.side_effect = RuntimeError("DB_PASSWORD=hidden")
        response = self.client.get("/health/ready")
        self.assert_error(response, 503, "DATABASE_NOT_READY")
        self.assertNotIn("hidden", response.text)

    def test_readiness_requires_email_approval_migration(self):
        for versions, expected_status in (([1, 2], 503), ([1, 2, 3], 503), ([1, 2, 3, 4], 503), ([1, 2, 3, 4, 5], 503), ([1, 2, 3, 4, 5, 6], 503), ([1, 2, 3, 4, 5, 6, 7], 200)):
            with self.subTest(versions=versions):
                tx = Mock()
                tx.one.return_value = {"version": "8.4.11"}
                tx.all.return_value = [{"version": value} for value in versions]

                @contextmanager
                def transaction():
                    yield tx

                self.harness.services.database.transaction.side_effect = transaction
                self.assertEqual(self.client.get("/health/ready").status_code, expected_status)


class ScanReceiptApiTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.request_id = str(uuid4())
        self.harness.receipts.get.return_value = {
            "request_id": self.request_id, "serving_id": 12, "kind": "EMPLOYEE",
            "quantity": 1, "served_at": "2026-09-09T12:00:00+00:00",
            "employee_name": "Fictional Employee", "employee_id": 7,
            "photo_available": True, "visitor_name": None,
        }
        self.harness.receipts.photo_key.return_value = "b" * 64 + ".png"

    def test_receipt_and_photo_require_authenticated_serving_role(self):
        for suffix in ("receipt", "photo", "result"):
            self.assert_error(self.client.get(f"/api/scans/{self.request_id}/{suffix}"), 401, "AUTHENTICATION_REQUIRED")
        self.harness.login("auditor")
        for suffix in ("receipt", "photo", "result"):
            self.assert_error(self.client.get(f"/api/scans/{self.request_id}/{suffix}"), 403, "ROLE_REQUIRED")
        self.harness.receipts.get.assert_not_called()
        self.harness.receipts.photo_key.assert_not_called()
        self.harness.receipts.result.assert_not_called()
        self.harness.storage.read.assert_not_called()

    def test_waiter_receipt_uses_authenticated_context_and_private_cache_policy(self):
        self.harness.login("waiter")
        response = self.client.get(f"/api/scans/{self.request_id}/receipt")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["employee_name"], "Fictional Employee")
        self.assertEqual(response.headers["cache-control"], "no-store")
        context, request_id = self.harness.receipts.get.call_args.args
        self.assertEqual(self.harness.staff.current(context)["staff_id"], 2)
        self.assertEqual(str(request_id), self.request_id)
        self.harness.queries.employee.assert_not_called()

    def test_admin_can_view_own_receipt_through_same_service(self):
        self.harness.login()
        response = self.client.get(f"/api/scans/{self.request_id}/receipt")
        self.assertEqual(response.status_code, 200)
        context = self.harness.receipts.get.call_args.args[0]
        self.assertEqual(self.harness.staff.current(context)["staff_id"], 1)

    def test_photo_scope_is_checked_before_private_storage_read(self):
        self.harness.login("waiter")
        self.harness.receipts.photo_key.side_effect = DomainError("SCAN_RECEIPT_NOT_FOUND")
        response = self.client.get(f"/api/scans/{self.request_id}/photo")
        self.assert_error(response, 404, "SCAN_RECEIPT_NOT_FOUND")
        self.harness.storage.read.assert_not_called()
        self.harness.queries.employee.assert_not_called()

    def test_authorized_receipt_photo_returns_only_image(self):
        self.harness.login("waiter")
        response = self.client.get(f"/api/scans/{self.request_id}/photo")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"fake-private-photo")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.harness.storage.read.assert_called_once_with("b" * 64 + ".png")
        self.assertNotIn("b" * 64, response.text)

    def test_receipt_identifiers_are_validated_before_services(self):
        self.harness.login("waiter")
        for suffix in ("receipt", "photo", "result"):
            self.assert_error(self.client.get(f"/api/scans/not-a-uuid/{suffix}"), 422, "INVALID_INPUT")
        self.harness.receipts.get.assert_not_called()
        self.harness.receipts.photo_key.assert_not_called()
        self.harness.receipts.result.assert_not_called()

    def test_result_recovery_returns_committed_ids_without_recording_another_meal(self):
        self.harness.login("waiter")
        original = {"approved": True, "code": "APPROVED", "request_id": self.request_id,
                    "serving_id": 12, "meal_ids": [13], "duplicate": True}
        self.harness.receipts.result.return_value = original
        response = self.client.get(f"/api/scans/{self.request_id}/result")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), original)
        self.assertEqual(self.harness.meals.meals, [])
        self.assertEqual(self.harness.meals.scans, [])
        context = self.harness.receipts.result.call_args.args[0]
        self.assertEqual(self.harness.staff.current(context)["staff_id"], 2)

    def test_pending_recovery_cannot_be_reported_as_approval(self):
        self.harness.login("waiter")
        self.harness.receipts.result.return_value = {"approved": False, "code": "PROCESSING_UNCONFIRMED",
            "request_id": self.request_id, "serving_id": None, "meal_ids": [], "duplicate": True}
        response = self.client.get(f"/api/scans/{self.request_id}/result")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["approved"])
        self.assertEqual(response.json()["code"], "PROCESSING_UNCONFIRMED")
        self.assertEqual(self.harness.meals.meals, [])

    def test_receipt_failure_never_exposes_database_or_photo_details(self):
        self.harness.login("waiter")
        secret = secrets.token_urlsafe(32)
        self.harness.receipts.get.side_effect = RuntimeError("SQL password " + secret)
        response = self.client.get(f"/api/scans/{self.request_id}/receipt")
        self.assert_error(response, 503, "SERVICE_UNAVAILABLE")
        self.assertNotIn(secret, response.text)


class DevelopmentDataApiTests(ApiTestCase):
    def test_waiter_and_anonymous_cannot_access_test_gallery_or_images(self):
        for path in ("/api/development/test-data", "/api/development/test-data/qrs/101"):
            self.assert_error(self.client.get(path), 401, "AUTHENTICATION_REQUIRED")
        self.harness.login("waiter")
        for path in ("/api/development/test-data", "/api/development/test-data/qrs/101"):
            self.assert_error(self.client.get(path), 403, "ROLE_REQUIRED")
        self.harness.development_seed.manifest.assert_not_called()
        self.harness.development_seed.export_svg.assert_not_called()

    def test_admin_can_view_gallery_and_fixture_only_svg(self):
        self.harness.login()
        response = self.client.get("/api/development/test-data")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"employees": [], "master": None, "waiters": []})
        image = self.client.get("/api/development/test-data/qrs/101")
        self.assertEqual(image.status_code, 200)
        self.assertEqual(image.headers["content-type"], "image/svg+xml")
        self.assertIn("TEST-QR-101.svg", image.headers["content-disposition"])
        self.assertEqual(image.headers["cache-control"], "no-store")
        self.assertEqual(self.harness.development_seed.export_svg.call_args.args[1], 101)
        self.harness.services.qr.export_svg.assert_not_called()

    def test_nonfixture_credentials_cannot_use_gallery_export(self):
        self.harness.login()
        self.harness.development_seed.export_svg.side_effect = DomainError("DEVELOPMENT_QR_NOT_FOUND")
        self.assert_error(self.client.get("/api/development/test-data/qrs/999"), 404, "DEVELOPMENT_QR_NOT_FOUND")
        self.harness.services.qr.export_svg.assert_not_called()

    def test_gallery_visibility_is_admin_only(self):
        self.harness.login("waiter")
        self.assertFalse(self.client.get("/api/catalog").json()["development_test_data"])
        self.harness.login()
        self.assertTrue(self.client.get("/api/catalog").json()["development_test_data"])

    def test_gallery_disabled_for_production_or_real_email_configuration(self):
        for changes in (
            {"environment": "production", "app_origin": "https://testserver", "cookie_secure": True},
            {"email_backend": "ses"},
            {"email_send_enabled": True},
        ):
            with self.subTest(configuration=list(changes)):
                harness = ApiHarness(replace(self.harness.runtime, **changes))
                self.addCleanup(harness.client.close)
                harness.login()
                for path in ("/api/development/test-data", "/api/development/test-data/qrs/101"):
                    response = harness.client.get(path)
                    self.assertEqual(response.status_code, 404)
                self.assertFalse(harness.client.get("/api/catalog").json()["development_test_data"])
                harness.development_seed.manifest.assert_not_called()
                harness.development_seed.export_svg.assert_not_called()

    def test_seed_is_not_exposed_as_a_public_api_mutation(self):
        self.harness.login()
        self.assertEqual(self.harness.post("/api/development/test-data", json={}).status_code, 405)


class ProductionHttpApiTests(unittest.TestCase):
    def setUp(self):
        runtime = RuntimeSettings(
            csrf_secret=secrets.token_bytes(32), login_rate_secret=secrets.token_bytes(32),
            environment="production", app_origin="https://testserver", allowed_hosts=("testserver",),
            cookie_secure=True, session_cookie="__Host-meal_session", photo_backend="s3", email_backend="ses",
        )
        self.harness = ApiHarness(runtime)
        self.addCleanup(self.harness.client.close)

    def test_production_serves_hsts_and_secure_cookies(self):
        response = self.harness.login()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["strict-transport-security"], "max-age=31536000")
        cookies = response.headers.get_list("set-cookie")
        self.assertTrue(all("Secure" in cookie for cookie in cookies))

    def test_production_rejects_http_before_service(self):
        response = self.harness.client.get("http://testserver/health/live")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "HTTPS_REQUIRED")

    def test_production_email_previews_are_disabled(self):
        self.harness.login()
        response = self.harness.client.get("/api/email-queue/1/preview")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "EMAIL_PREVIEW_DISABLED")
        self.harness.services.email_queue.preview.assert_not_called()


if __name__ == "__main__":
    unittest.main()
