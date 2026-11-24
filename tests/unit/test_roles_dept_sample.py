import io
from datetime import date, datetime, timezone
from unittest.mock import Mock, patch
from uuid import uuid4
from openpyxl import load_workbook

from meal_management.errors import DomainError
from meal_management.visitor_batches import read_rows, validate_rows, generate_visitor_sample_excel, generate_visitor_sample_csv
from test_api import ApiHarness, ApiTestCase


class RolesDepartmentSampleSuite(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.harness = ApiHarness()
        self.client = self.harness.client
        self.addCleanup(self.client.close)
        self.batches = Mock()
        self.harness.app.state.visitor_batches = self.batches
        self.batches.list.return_value = {"items": [], "next_cursor": None}
        self.batches.create.return_value = {"batch_id": 1, "duplicate": False}

    def test_super_admin_exclusive_endpoints_reject_admin_and_waiter(self):
        exclusive_endpoints = [
            ("GET", "/api/staff", None),
            ("POST", "/api/staff", {"email": "new@example.com", "display_name": "New", "password": "Password123456!", "roles": ["ADMIN"]}),
            ("PATCH", "/api/staff/1", {"display_name": "Updated"}),
            ("PATCH", "/api/staff/1/roles", {"roles": ["SUPER_ADMIN"]}),
            ("PATCH", "/api/staff/1/active", {"is_active": False}),
            ("POST", "/api/catalog/departments", {"code": "ENG", "name": "Engineering"}),
            ("PATCH", "/api/catalog/departments/1", {"code": "ENG", "name": "Engineering"}),
            ("PATCH", "/api/catalog/departments/1/active", {"is_active": False}),
            ("POST", "/api/catalog/meal-types", {"code": "SNACK", "name": "Snack"}),
            ("PATCH", "/api/catalog/meal-types/1", {"code": "SNACK", "name": "Snack"}),
            ("PATCH", "/api/catalog/meal-types/1/active", {"is_active": False}),
            ("POST", "/api/catalog/locations", {"code": "LOC2", "name": "Location 2"}),
            ("PATCH", "/api/catalog/locations/1", {"code": "LOC2", "name": "Location 2"}),
            ("PATCH", "/api/catalog/locations/1/active", {"is_active": False}),
            ("POST", "/api/catalog/scanners", {"code": "SCN2", "name": "Scanner 2"}),
            ("PATCH", "/api/catalog/scanners/1", {"code": "SCN2", "name": "Scanner 2"}),
            ("PATCH", "/api/catalog/scanners/1/active", {"is_active": False}),
            ("GET", "/api/scanner-phones", None),
            ("POST", "/api/scanner-phones", {"label": "Counter", "active_until": "2026-11-30"}),
            ("POST", "/api/scanner-phones/1/revoke", None),
            ("POST", "/api/employees/meal-types/preview", {"employee_ids": [1], "meal_type_ids": [1]}),
            ("POST", "/api/employees/meal-types", {"employee_ids": [1], "meal_type_ids": [1], "expected_changes": 1}),
            ("DELETE", "/api/employees/1", {"reason": "Departed"}),
            ("POST", "/api/employees/bulk-remove", {"employee_ids": [1], "reason": "Cleanup"}),
            ("POST", "/api/meals/1/meal-type", {"meal_type_id": 1, "reason": "Correction"}),
            ("GET", "/api/reports/meals/export?from=2026-11-01&through=2026-11-05", None),
        ]

        for method, path, body in exclusive_endpoints:
            with self.subTest(role="anonymous", method=method, path=path):
                self.client.cookies.clear()
                response = self.client.request(method, path, json=body) if body else self.client.request(method, path)
                self.assertIn(response.status_code, {401, 403})
                if response.status_code == 403:
                    self.assertIn(response.json()["error"]["code"], {"CSRF_REJECTED", "ORIGIN_REJECTED"})
                else:
                    self.assertEqual(response.json()["error"]["code"], "AUTHENTICATION_REQUIRED")

            with self.subTest(role="waiter", method=method, path=path):
                self.harness.login("waiter")
                response = self.client.request(method, path, json=body, headers=self.harness.headers()) if body else self.client.request(method, path, headers=self.harness.headers())
                self.assertEqual(response.status_code, 403, f"Waiter should get 403 for {path}")
                self.assertEqual(response.json()["error"]["code"], "ROLE_REQUIRED")

            with self.subTest(role="admin", method=method, path=path):
                self.harness.login("admin")
                response = self.client.request(method, path, json=body, headers=self.harness.headers()) if body else self.client.request(method, path, headers=self.harness.headers())
                self.assertEqual(response.status_code, 403, f"Admin should get 403 for {path}")
                self.assertEqual(response.json()["error"]["code"], "ROLE_REQUIRED")

    def test_admin_shared_endpoints_are_accessible_to_both_admin_and_super_admin(self):
        self.harness.login("admin")
        self.assertEqual(self.client.get("/api/reports/meals?start=2026-11-01T00:00:00Z&end=2026-11-05T00:00:00Z").status_code, 200)
        self.assertEqual(self.client.get("/api/reports/totals?start=2026-11-01T00:00:00Z&end=2026-11-05T00:00:00Z").status_code, 200)
        self.assertEqual(self.client.get("/api/reports/scans?start=2026-11-01T00:00:00Z&end=2026-11-05T00:00:00Z").status_code, 200)
        self.assertEqual(self.client.get("/api/employees").status_code, 200)
        self.assertEqual(self.client.get("/api/master-qrs").status_code, 200)
        self.assertEqual(self.client.get("/api/visitor-batches").status_code, 200)
        self.assertEqual(self.client.get("/api/email-queue").status_code, 200)
        self.assertEqual(self.client.get("/api/visitor-batches/sample.xlsx").status_code, 200)
        self.assertEqual(self.client.get("/api/visitor-batches/sample.csv").status_code, 200)

        self.harness.login("super_admin")
        self.assertEqual(self.client.get("/api/reports/meals?start=2026-11-01T00:00:00Z&end=2026-11-05T00:00:00Z").status_code, 200)
        self.assertEqual(self.client.get("/api/reports/totals?start=2026-11-01T00:00:00Z&end=2026-11-05T00:00:00Z").status_code, 200)
        self.assertEqual(self.client.get("/api/reports/scans?start=2026-11-01T00:00:00Z&end=2026-11-05T00:00:00Z").status_code, 200)
        self.assertEqual(self.client.get("/api/employees").status_code, 200)
        self.assertEqual(self.client.get("/api/master-qrs").status_code, 200)
        self.assertEqual(self.client.get("/api/visitor-batches").status_code, 200)
        self.assertEqual(self.client.get("/api/email-queue").status_code, 200)
        self.assertEqual(self.client.get("/api/visitor-batches/sample.xlsx").status_code, 200)
        self.assertEqual(self.client.get("/api/visitor-batches/sample.csv").status_code, 200)

    def test_department_forcing_for_admin_and_choice_for_super_admin(self):
        body = {
            "company_name": "Vendors Inc", "contact_name": "Anita Roy", "email": "anita@example.test",
            "phone": "+919876543210", "meal_limit": 5, "meal_type_ids": [1], "department_id": 99,
        }
        self.harness.login("admin")
        response = self.harness.post("/api/master-qrs", json=body)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.harness.services.qr.issue_master.call_args.kwargs["department_id"], 1)

        self.harness.login("admin_no_dept")
        response = self.harness.post("/api/master-qrs", json=body)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "ADMIN_DEPARTMENT_REQUIRED")

        self.harness.login("super_admin")
        response = self.harness.post("/api/master-qrs", json=body)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.harness.services.qr.issue_master.call_args.kwargs["department_id"], 99)

        batch_body = {
            "request_id": str(uuid4()), "source_name": "visitors.xlsx", "expires_at": "2026-11-30T18:00:00+05:30",
            "meal_type_ids": [1], "department_id": 99,
            "rows": [{"row": 2, "sr_no": "1", "person_name": "P1", "company_name": "C1", "phone": "+919876543210", "email": ""}],
        }
        self.harness.app.state.visitor_batches = Mock()
        self.harness.app.state.visitor_batches.create.return_value = {"batch_id": 1, "duplicate": False}

        self.harness.login("admin")
        response = self.harness.post("/api/visitor-batches", json=batch_body)
        self.assertEqual(response.status_code, 201)
        args, kwargs = self.harness.app.state.visitor_batches.create.call_args
        self.assertEqual(args[5], 1)

        self.harness.login("admin_no_dept")
        response = self.harness.post("/api/visitor-batches", json=batch_body)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "ADMIN_DEPARTMENT_REQUIRED")

        self.harness.login("super_admin")
        response = self.harness.post("/api/visitor-batches", json=batch_body)
        self.assertEqual(response.status_code, 201)
        args, kwargs = self.harness.app.state.visitor_batches.create.call_args
        self.assertEqual(args[5], 99)

    def test_sample_excel_and_csv_generation_and_reimport(self):
        excel_bytes = generate_visitor_sample_excel()
        wb = load_workbook(io.BytesIO(excel_bytes))
        self.assertEqual(wb.sheetnames, ["Visitors", "Instructions"])
        sheet = wb["Visitors"]
        self.assertEqual([c.value for c in sheet[1]], ["Sr. No.", "Person Name", "Company Name", "Phone", "Email"])
        self.assertEqual(sheet["A2"].value, "1")
        self.assertEqual(sheet["A2"].data_type, "s")
        self.assertEqual(sheet["D2"].value, "+91 98765 43210")
        self.assertEqual(sheet["D2"].data_type, "s")

        rows = read_rows(excel_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "sample.xlsx")
        self.assertEqual(len(rows), 2)
        clean, errors = validate_rows(rows)
        self.assertEqual(len(clean), 2)
        self.assertEqual(errors, [])

        csv_bytes = generate_visitor_sample_csv()
        rows_csv = read_rows(csv_bytes, "text/csv", "sample.csv")
        self.assertEqual(len(rows_csv), 2)
        clean_csv, errors_csv = validate_rows(rows_csv)
        self.assertEqual(len(clean_csv), 2)
        self.assertEqual(errors_csv, [])

    def test_admin_with_no_department_blocked_from_visitor_qrs_only(self):
        self.harness.login("admin_no_dept")
        master_body = {
            "company_name": "Vendors Inc", "contact_name": "Anita Roy", "email": "anita@example.test",
            "phone": "+919876543210", "meal_limit": 5, "meal_type_ids": [1], "department_id": 1,
        }
        res_master = self.harness.post("/api/master-qrs", json=master_body)
        self.assertEqual(res_master.status_code, 403)
        self.assertEqual(res_master.json()["error"]["code"], "ADMIN_DEPARTMENT_REQUIRED")

        res_replace = self.harness.post("/api/master-qrs/1/replace", json=master_body)
        self.assertEqual(res_replace.status_code, 403)
        self.assertEqual(res_replace.json()["error"]["code"], "ADMIN_DEPARTMENT_REQUIRED")

        batch_body = {
            "request_id": str(uuid4()), "source_name": "visitors.xlsx", "expires_at": "2026-11-30T18:00:00+05:30",
            "meal_type_ids": [1], "department_id": 1,
            "rows": [{"row": 2, "sr_no": "1", "person_name": "P1", "company_name": "C1", "phone": "+919876543210", "email": ""}],
        }
        res_batch = self.harness.post("/api/visitor-batches", json=batch_body)
        self.assertEqual(res_batch.status_code, 403)
        self.assertEqual(res_batch.json()["error"]["code"], "ADMIN_DEPARTMENT_REQUIRED")

        self.assertEqual(self.client.get("/api/employees").status_code, 200)
        self.assertEqual(self.client.get("/api/reports/meals?start=2026-11-01T00:00:00Z&end=2026-11-05T00:00:00Z").status_code, 200)
        self.assertEqual(self.client.get("/api/reports/scans?start=2026-11-01T00:00:00Z&end=2026-11-05T00:00:00Z").status_code, 200)
        self.assertEqual(self.client.get("/api/email-queue").status_code, 200)
        self.assertEqual(self.client.get("/api/master-qrs").status_code, 200)
        self.assertEqual(self.client.get("/api/visitor-batches").status_code, 200)
        self.assertEqual(self.client.get("/api/visitor-batches/sample.xlsx").status_code, 200)
        self.assertEqual(self.client.get("/api/visitor-batches/sample.csv").status_code, 200)

