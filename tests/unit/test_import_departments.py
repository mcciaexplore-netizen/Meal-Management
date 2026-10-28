import re
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import uuid4

from meal_management.employees import FACILITY_DEPARTMENTS, EmployeeService, department_key
from meal_management.errors import DomainError
from meal_management.models import Actor
from test_account_services import TransactionDatabase
from test_api import ApiTestCase
from test_employee_sheet import XLSX, read, workbook

FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "src" / "facility_departments.js"


class DepartmentNameTests(unittest.TestCase):
    def setUp(self):
        self.tx = Mock()
        self.tx.insert.return_value = 41
        self.tx.all.return_value = [
            {"id": 3, "name": "Exhibition Centre", "is_active": 1},
            {"id": 9, "name": "AI Studio", "is_active": 0},
            {"id": 11, "name": "Quality Office", "is_active": 1},
        ]
        self.service = EmployeeService(TransactionDatabase(self.tx), Mock())
        self.actor = Actor(7, frozenset({"ADMIN"}))

    def resolve(self, *names):
        with patch("meal_management.employees.audit") as audit:
            return self.service._import_departments(self.tx, self.actor, set(names)), audit

    def test_built_in_facilities_match_the_employee_form(self):
        names = re.findall(r'name: "([^"]+)"', FRONTEND.read_text())
        self.assertEqual(tuple(names), FACILITY_DEPARTMENTS)

    def test_keys_ignore_case_accents_and_extra_spaces(self):
        self.assertEqual(department_key("  Éxhibition   CENTRE "), "exhibition centre")
        self.assertEqual(department_key("Rubber &  Polymer\tTesting Lab"), "rubber & polymer testing lab")

    def test_existing_active_names_resolve_without_writes(self):
        resolved, audit = self.resolve("exhibition centre", "Quality  Office")
        self.assertEqual(resolved, {"exhibition centre": 3, "quality office": 11})
        self.tx.insert.assert_not_called()
        audit.assert_not_called()

    def test_a_built_in_facility_is_created_once_under_its_proper_name(self):
        resolved, audit = self.resolve("auditorium", "AUDITORIUM")
        self.assertEqual(resolved, {"auditorium": 41})
        self.assertEqual(self.tx.insert.call_count, 1)
        self.assertEqual(self.tx.insert.call_args.args[1], ("Auditorium", True))
        audit.assert_called_once_with(self.tx, 7, "DEPARTMENT_CREATED", "departments", 41, after={"name": "Auditorium"})

    def test_inactive_and_unknown_names_are_refused(self):
        for name, code in (("ai studio", "DEPARTMENT_UNAVAILABLE"), ("Department 7", "DEPARTMENT_NOT_FOUND")):
            with self.subTest(name=name), self.assertRaisesRegex(DomainError, code):
                self.resolve(name)
        self.tx.insert.assert_not_called()

    def test_no_names_need_no_queries(self):
        self.assertEqual(self.resolve()[0], {})
        self.tx.all.assert_not_called()


class BulkRowShapeTests(unittest.TestCase):
    def setUp(self):
        self.service = EmployeeService(Mock(), Mock())

    def row(self, **changes):
        values = {"employee_code": "E1", "full_name": "Asha", "email": "asha@mcciapune.in", "company_name": "MCCIA", "phone": "+91 98765 43210", "meal_type_ids": [2]}
        values.update(changes)
        return values

    def test_rows_without_department_or_with_department_id_are_checked_before_the_database(self):
        for row, code in (
            (self.row(department_id=4), "INVALID_EMPLOYEE_BATCH"),
            (self.row(department="   "), "INVALID_DEPARTMENT_NAME"),
            (self.row(department="x" * 101), "INVALID_DEPARTMENT_NAME"),
        ):
            with self.subTest(code=code), self.assertRaisesRegex(DomainError, code):
                self.service.register_bulk(Mock(), uuid4(), [row])
        self.service.db.transaction.assert_not_called()


class ExcelTemplateTests(unittest.TestCase):
    def test_excel_rows_keep_department_and_department_id_columns_for_the_browser(self):
        header = ["employee_code", "full_name", "email", "company_name", "phone", "department_id", "department", "meal_types"]
        rows = read(workbook([["E-1", "Asha", "asha@mcciapune.in", "MCCIA", "9876543210", 7, "Exhibition Centre", None],
                              ["E-2", "Ravi", "ravi@mcciapune.in", "MCCIA", "9876543211", 16, None, None]], header=header))
        self.assertEqual(rows[0], header)
        self.assertEqual(rows[1][5:], ["7", "Exhibition Centre", ""])
        self.assertEqual(rows[2][5:], ["16", "", ""])


class BulkApiTests(ApiTestCase):
    def body(self, **changes):
        row = {"employee_code": "E1", "full_name": "Asha", "email": "asha@mcciapune.in", "company_name": "MCCIA", "phone": "+91 98765 43210", "meal_type_ids": [2]}
        row.update(changes)
        return {"request_id": str(uuid4()), "employees": [row]}

    def test_department_name_is_optional_and_department_id_is_refused(self):
        self.harness.login()
        self.harness.services.employees.register_bulk.return_value = {"batch_id": 4, "employees": [], "replayed": False}
        for body in (self.body(), self.body(department="Exhibition Centre")):
            response = self.harness.post("/api/employees/bulk", json=body)
            self.assertEqual(response.status_code, 201, response.text)
            self.assertEqual(self.harness.services.employees.register_bulk.call_args.args[2], body["employees"])
        self.assert_error(self.harness.post("/api/employees/bulk", json=self.body(department_id=4)), 422, "INVALID_INPUT")
        self.assert_error(self.harness.post("/api/employees/bulk", json=self.body(department="")), 422, "INVALID_INPUT")


if __name__ == "__main__":
    unittest.main()
