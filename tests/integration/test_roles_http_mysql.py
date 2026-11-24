import re
import secrets
from uuid import uuid4

from test_mysql_services import MealServicesFixture


SPEC_SUPER_ADMIN_ROUTES = {
    ("GET", "/api/staff"), ("POST", "/api/staff"), ("PATCH", "/api/staff/{staff_id}"),
    ("PATCH", "/api/staff/{staff_id}/roles"), ("PATCH", "/api/staff/{staff_id}/active"),
    ("POST", "/api/catalog/departments"), ("PATCH", "/api/catalog/departments/{department_id}"),
    ("GET", "/api/catalog/departments/{department_id}/usage"),
    ("PATCH", "/api/catalog/departments/{department_id}/active"),
    ("POST", "/api/catalog/locations"), ("POST", "/api/catalog/meal-types"), ("POST", "/api/catalog/scanners"),
    ("PATCH", "/api/catalog/{kind}/{item_id}"), ("PATCH", "/api/catalog/{kind}/{item_id}/active"),
    ("GET", "/api/scanner-phones"), ("POST", "/api/scanner-phones"), ("POST", "/api/scanner-phones/{phone_id}/revoke"),
    ("POST", "/api/employees/meal-types/preview"), ("POST", "/api/employees/meal-types"),
    ("POST", "/api/employees/bulk-remove"), ("DELETE", "/api/employees/{employee_id}"),
    ("POST", "/api/meals/{meal_id}/meal-type"),
    ("GET", "/api/reports/meals/export"),
}


def _uses(route, name):
    pending = list(getattr(route, "dependant", None).dependencies if hasattr(route, "dependant") else [])
    while pending:
        item = pending.pop()
        if getattr(item.call, "__name__", "") == name:
            return True
        pending.extend(item.dependencies)
    return False


class RolesOverHttpMySQLTests(MealServicesFixture):
    def setUp(self):
        from fastapi.testclient import TestClient

        from meal_management.admin_api import create_app
        from meal_management.application import create_services
        from meal_management.runtime import RuntimeSettings

        self.runtime = RuntimeSettings(
            csrf_secret=secrets.token_bytes(32),
            login_rate_secret=secrets.token_bytes(32),
            app_origin="http://localhost:8000",
            scanner_origin="http://localhost:8001",
            allowed_hosts=("localhost",),
        )
        self.app = create_app(services=create_services(self.settings), runtime=self.runtime)
        self.http = TestClient(self.app, base_url=self.runtime.app_origin)
        self.addCleanup(self.http.close)

    def account(self, role, department_id=None):
        password = secrets.token_urlsafe(24)
        email = f"{role.lower().replace('_', '-')}-{uuid4().hex[:10]}@example.test"
        staff_id = self.staff.create_staff(self.admin_context, role, email, password, {role}, department_id=department_id)
        return staff_id, email, password

    def headers(self):
        token = self.http.get("/api/auth/csrf").json()["csrf_token"]
        return {"Origin": self.runtime.app_origin, "X-CSRF-Token": token}

    def sign_in(self, email, password):
        self.http.cookies.clear()
        response = self.http.post("/api/auth/login", json={"email": email, "password": password}, headers=self.headers())
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def call(self, method, path):
        concrete = re.sub(r"\{kind\}", "locations", path)
        concrete = re.sub(r"\{[a-z_]+\}", "999999999", concrete)
        if method == "GET":
            return self.http.get(concrete, headers=self.headers())
        return self.http.request(method, concrete, json={}, headers=self.headers())

    def super_admin_routes(self):
        found = set()
        for route in self.app.routes:
            if _uses(route, "super_admin"):
                for method in route.methods - {"HEAD"}:
                    found.add((method, route.path))
        return found

    def test_route_table_matches_spec(self):
        self.assertEqual(self.super_admin_routes(), SPEC_SUPER_ADMIN_ROUTES)

    def test_admin_gets_403_on_every_super_admin_route_and_super_admin_does_not(self):
        _, admin_email, admin_password = self.account("ADMIN", self.department_id)
        _, super_email, super_password = self.account("SUPER_ADMIN")
        routes = sorted(self.super_admin_routes())
        self.assertGreaterEqual(len(routes), len(SPEC_SUPER_ADMIN_ROUTES))
        self.sign_in(admin_email, admin_password)
        for method, path in routes:
            with self.subTest(role="ADMIN", method=method, path=path):
                response = self.call(method, path)
                self.assertEqual(response.status_code, 403, response.text)
                self.assertEqual(response.json()["error"]["code"], "ROLE_REQUIRED")
        self.sign_in(super_email, super_password)
        for method, path in routes:
            with self.subTest(role="SUPER_ADMIN", method=method, path=path):
                response = self.call(method, path)
                self.assertNotIn(response.status_code, {401, 403}, response.text)

    def test_admin_keeps_shared_powers(self):
        _, email, password = self.account("ADMIN", self.department_id)
        self.sign_in(email, password)
        for path in ("/api/employees", "/api/master-qrs", "/api/visitor-batches", "/api/email-queue",
                     "/api/visitor-batches/sample.xlsx", "/api/visitor-batches/sample.csv"):
            with self.subTest(path=path):
                self.assertEqual(self.http.get(path, headers=self.headers()).status_code, 200)
        for path in ("/api/reports/meals", "/api/reports/scans"):
            with self.subTest(path=path):
                response = self.http.get(path + "?start=2026-01-01T00:00:00Z&end=2026-01-02T00:00:00Z", headers=self.headers())
                self.assertEqual(response.status_code, 200, response.text)

    def test_roles_are_read_from_the_database_on_every_request(self):
        staff_id, email, password = self.account("ADMIN", self.department_id)
        self.sign_in(email, password)
        self.assertEqual(self.http.get("/api/staff", headers=self.headers()).status_code, 403)
        self.staff.set_roles(self.admin_context, staff_id, ["SUPER_ADMIN"])
        self.assertEqual(self.http.get("/api/staff", headers=self.headers()).status_code, 200)
        self.assertIn("SUPER_ADMIN", self.http.get("/api/auth/me").json()["roles"])
        self.staff.set_roles(self.admin_context, staff_id, ["ADMIN"])
        self.assertEqual(self.http.get("/api/staff", headers=self.headers()).status_code, 403)

    def master_body(self, department_id):
        return {
            "company_name": "Fictional Visitor Company", "contact_name": "Fictional Contact",
            "phone": "+91 90000 00001", "meal_limit": 2, "meal_type_ids": [self.meal_type_id],
            "department_id": department_id,
        }

    def test_department_is_forced_for_admin_and_chosen_by_super_admin(self):
        other = self.employees.create_department(self.admin_context, "Other " + uuid4().hex[:8])
        _, email, password = self.account("ADMIN", self.department_id)
        identity = self.sign_in(email, password)
        self.assertEqual(identity["department_id"], self.department_id)
        created = self.http.post("/api/master-qrs", json=self.master_body(other), headers=self.headers())
        self.assertEqual(created.status_code, 201, created.text)
        forced = self.scalar("SELECT department_id FROM master_qr_allocations WHERE qr_id = %s", (created.json()["qr_id"],))
        self.assertEqual(forced, self.department_id)

        _, email, password = self.account("SUPER_ADMIN")
        self.sign_in(email, password)
        created = self.http.post("/api/master-qrs", json=self.master_body(other), headers=self.headers())
        self.assertEqual(created.status_code, 201, created.text)
        chosen = self.scalar("SELECT department_id FROM master_qr_allocations WHERE qr_id = %s", (created.json()["qr_id"],))
        self.assertEqual(chosen, other)

    def test_admin_without_department_is_blocked_from_visitor_qrs_only(self):
        _, email, password = self.account("ADMIN")
        self.sign_in(email, password)
        response = self.http.post("/api/master-qrs", json=self.master_body(self.department_id), headers=self.headers())
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "ADMIN_DEPARTMENT_REQUIRED")
        self.assertIn("super admin", response.json()["error"]["message"])
        batch = self.http.post("/api/visitor-batches", json={
            "request_id": str(uuid4()), "source_name": "list.csv", "expires_at": "2030-01-01T00:00:00Z",
            "meal_type_ids": [self.meal_type_id], "department_id": self.department_id,
            "rows": [{"row": 2, "sr_no": "1", "person_name": "A", "company_name": "B", "phone": "+91 90000 00002"}],
        }, headers=self.headers())
        self.assertEqual(batch.status_code, 403, batch.text)
        self.assertEqual(batch.json()["error"]["code"], "ADMIN_DEPARTMENT_REQUIRED")
        self.assertEqual(self.http.get("/api/employees", headers=self.headers()).status_code, 200)

    def test_staff_account_cannot_hold_both_admin_roles(self):
        from meal_management.errors import DomainError

        with self.assertRaisesRegex(DomainError, "INVALID_ROLES"):
            self.account_with({"SUPER_ADMIN", "ADMIN"})
        _, email, password = self.account("SUPER_ADMIN")
        self.sign_in(email, password)
        response = self.http.post("/api/staff", json={
            "display_name": "Both", "email": f"both-{uuid4().hex[:8]}@example.test",
            "password": secrets.token_urlsafe(24), "roles": ["SUPER_ADMIN", "ADMIN"],
        }, headers=self.headers())
        self.assertEqual(response.status_code, 422)

    def account_with(self, roles):
        return self.staff.create_staff(self.admin_context, "Both", f"both-{uuid4().hex[:8]}@example.test",
                                       secrets.token_urlsafe(24), roles)
