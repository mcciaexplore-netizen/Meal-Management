import secrets
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from test_mysql_services import MealServicesFixture


class IndependentApplicationsMySQLTests(MealServicesFixture):
    def runtime(self):
        from meal_management.runtime import RuntimeSettings

        return RuntimeSettings(
            csrf_secret=secrets.token_bytes(32),
            login_rate_secret=secrets.token_bytes(32),
            app_origin="http://localhost:8000",
            scanner_origin="http://localhost:8001",
            allowed_hosts=("localhost",),
        )

    def client(self, application, runtime):
        from fastapi.testclient import TestClient

        from meal_management.admin_api import create_app as create_admin
        from meal_management.application import create_services
        from meal_management.scanner_api import create_app as create_scanner

        factory = create_admin if application == "admin" else create_scanner
        app = factory(services=create_services(self.settings), runtime=runtime)
        return TestClient(app, base_url=app.state.runtime.app_origin)

    def scan_headers(self, client, runtime):
        response = client.get("/api/scanner/session")
        self.assertEqual(response.status_code, 200)
        return {"Origin": runtime.scanner_origin, "X-CSRF-Token": response.json()["csrf_token"]}

    def test_scanner_records_before_admin_starts_and_dashboard_reads_same_committed_records(self):
        registration, employee_token = self.employee()
        runtime = self.runtime()
        visitor = {
            "company_name": "Fictional Example Company",
            "name": "Fictional Visitor",
            "email": "visitor@example.test",
            "phone": "+91 90000 00000",
        }
        master = self.qr.issue_master(
            self.admin_context,
            company_name=visitor["company_name"],
            contact_name=visitor["name"],
            email=visitor["email"],
            phone=visitor["phone"],
            meal_limit=1,
        )
        first_body = {"request_id": str(uuid4()), "token": employee_token}
        master_body = {"request_id": str(uuid4()), "token": master.token}
        baseline = self.scalar("SELECT COALESCE(MAX(id), 0) FROM meals")
        start = datetime.now(timezone.utc) - timedelta(minutes=1)
        with self.client("scanner", runtime) as scanner:
            headers = self.scan_headers(scanner, runtime)
            first = scanner.post("/api/scanner/read", json=first_body, headers=headers)
            self.assertEqual(first.status_code, 200)
            self.assertTrue(first.json()["approved"])
            duplicate = scanner.post("/api/scanner/read", json=first_body, headers=headers)
            self.assertTrue(duplicate.json()["duplicate"])
            self.assertEqual(duplicate.json()["meal_ids"], first.json()["meal_ids"])
            second = scanner.post("/api/scanner/read", json={**first_body, "request_id": str(uuid4())}, headers=headers)
            self.assertTrue(second.json()["approved"])
            self.assertNotEqual(second.json()["meal_ids"], first.json()["meal_ids"])
            recorded = scanner.post("/api/scanner/read", json=master_body, headers=headers)
            self.assertEqual(recorded.status_code, 200)
            self.assertTrue(recorded.json()["approved"])
            scanner_cookie = scanner.cookies.get("meal_scanner")
            expected_ids = set(first.json()["meal_ids"] + second.json()["meal_ids"] + recorded.json()["meal_ids"])

        with self.client("scanner", runtime) as restarted_scanner:
            restarted_scanner.cookies.set("meal_scanner", scanner_cookie)
            missing_marker = restarted_scanner.get("/api/scanner/recovery")
            self.assertEqual(missing_marker.status_code, 200)
            self.assertEqual(missing_marker.json(), {"request": {"request_id": master_body["request_id"], "quantity": 1}})
            recovered = restarted_scanner.get("/api/scanner/requests/" + master_body["request_id"] + "/result")
            self.assertTrue(recovered.json()["approved"])
            self.assertEqual(recovered.json()["meal_ids"], recorded.json()["meal_ids"])
            retried = restarted_scanner.post("/api/scanner/read", json=master_body,
                                              headers=self.scan_headers(restarted_scanner, runtime))
            self.assertEqual(retried.status_code, 200)
            self.assertTrue(retried.json()["approved"])
            self.assertTrue(retried.json()["duplicate"])
            self.assertEqual(retried.json()["meal_ids"], recorded.json()["meal_ids"])
            exhausted = restarted_scanner.post("/api/scanner/read", json={**master_body, "request_id": str(uuid4())},
                                                headers=self.scan_headers(restarted_scanner, runtime))
            self.assertEqual(exhausted.status_code, 200)
            self.assertFalse(exhausted.json()["approved"])
            self.assertEqual(exhausted.json()["code"], "QR_EXPIRED")

        with self.client("admin", runtime) as admin:
            admin.cookies.set(runtime.session_cookie, self.admin_context.session_token)
            filters = {"start": start.isoformat(), "end": (start + timedelta(days=1)).isoformat(), "after_id": baseline}
            history = admin.get("/api/reports/meals", params=filters)
            self.assertEqual(history.status_code, 200)
            rows = history.json()["items"]
            self.assertEqual({row["meal_id"] for row in rows}, expected_ids)
            self.assertEqual(len(rows), 3)
            for row in rows:
                self.assertEqual(row["serving_quantity"], 1)
                self.assertEqual(row["waiter_name"], "Meal Scanner")
                self.assertIsNone(row["authorized_by"])
                self.assertIsNone(row["admin_name"])
                self.assertEqual(datetime.fromisoformat(row["served_at"]).utcoffset(), timedelta(0))
            employees = [row for row in rows if row["kind"] == "EMPLOYEE"]
            self.assertEqual(len(employees), 2)
            self.assertTrue(all(row["employee_id"] == registration.employee_id for row in employees))
            saved_visitor = next(row for row in rows if row["kind"] == "MASTER")
            for source, stored in (("company_name", "visitor_company_name"), ("name", "visitor_name"),
                                   ("email", "visitor_email"), ("phone", "visitor_phone")):
                self.assertEqual(saved_visitor[stored], visitor[source])
            totals = admin.get("/api/reports/totals", params={"start": filters["start"], "end": filters["end"]})
            self.assertEqual(totals.status_code, 200)
            self.assertGreaterEqual(totals.json()["totals"]["employee_meals"], 2)
            self.assertGreaterEqual(totals.json()["totals"]["visitor_meals"], 1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM meals WHERE id > %s", (baseline,)), 3)
        self.assertEqual(self.scalar("SELECT meals_used FROM master_qr_allocations WHERE qr_id = %s", (master.qr_id,)), 1)

    def test_scanner_credentials_do_not_grant_administration_or_expose_admin_routes(self):
        runtime = self.runtime()
        with self.client("scanner", runtime) as scanner, self.client("admin", runtime) as admin:
            headers = self.scan_headers(scanner, runtime)
            cookie = scanner.cookies.get("meal_scanner")
            admin.cookies.set("meal_scanner", cookie)
            self.assertEqual(admin.get("/api/employees").status_code, 401)
            admin.cookies.set(runtime.session_cookie, cookie)
            self.assertEqual(admin.get("/api/employees").status_code, 401)
            for path in ("/api/auth/me", "/api/employees", "/api/master-qrs", "/api/reports/meals",
                         "/api/reports/totals", "/api/email-queue", "/assets/app.js", "/api/application"):
                with self.subTest(path=path):
                    self.assertEqual(scanner.get(path).status_code, 404)
            self.assertEqual(admin.get("/api/scanner/session").status_code, 404)
            scanner.cookies.set(runtime.session_cookie, self.admin_context.session_token)
            self.assertEqual(scanner.get("/api/employees").status_code, 404)
            self.assertEqual(scanner.post("/api/scanner/visitors", json={}, headers=headers).status_code, 404)
            response = scanner.post("/api/scanner/read", json={"request_id": str(uuid4()), "token": secrets.token_urlsafe(32)},
                                    headers={**headers, "Origin": runtime.app_origin})
            self.assertEqual(response.status_code, 403)
