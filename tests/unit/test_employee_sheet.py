import io
import unittest
from unittest.mock import Mock, patch

from meal_management.employee_sheet import read_employee_sheet
from meal_management.employees import EmployeeService
from meal_management.errors import DomainError
from test_api import ApiTestCase
from test_visitor_bulk import with_entry

HEADERS = ["employee_code", "full_name", "email", "company_name", "phone", "department_id"]
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def workbook(rows, header=HEADERS, formula=None):
    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    sheet.append(header)
    for row in rows:
        sheet.append(row)
    if formula:
        sheet[formula] = "=1+1"
    output = io.BytesIO()
    book.save(output)
    return output.getvalue()


def read(data, name="staff.xlsx", kind=XLSX):
    return read_employee_sheet(data, kind, name)


class EmployeeSheetTests(unittest.TestCase):
    def test_first_sheet_becomes_text_rows_with_numbers_as_plain_digits(self):
        data = workbook([["E-1", "Asha Rao", "asha@mcciapune.in", "MCCIA", 9876543210, 3],
                         ["E-2", "Ravi", "ravi@mcciapune.in", "MCCIA", "+91 98765 43211", 4.0],
                         [None, None, None, None, None, None]], header=HEADERS + ["meal_types", None])
        self.assertEqual(read(data), [
            HEADERS + ["meal_types"],
            ["E-1", "Asha Rao", "asha@mcciapune.in", "MCCIA", "9876543210", "3", ""],
            ["E-2", "Ravi", "ravi@mcciapune.in", "MCCIA", "+91 98765 43211", "4", ""],
        ])
        self.assertEqual(read(data, kind="application/octet-stream")[1][0], "E-1")

    def test_only_safe_xlsx_files_are_read(self):
        good = workbook([["E-1", "A", "a@mcciapune.in", "M", "9876543210", "3"]])
        cases = (
            (lambda: read(b""), "EMPLOYEE_FILE_REQUIRED"),
            (lambda: read(b"PK\x03\x04" + b"x" * 1_048_576), "EMPLOYEE_FILE_TOO_LARGE"),
            (lambda: read(good, name="staff.xls"), "EMPLOYEE_FILE_TYPE"),
            (lambda: read(good, name="staff.xlsm"), "EMPLOYEE_FILE_TYPE"),
            (lambda: read(b"employee_code\n", name="staff.csv", kind="text/csv"), "EMPLOYEE_FILE_TYPE"),
            (lambda: read(good, kind="text/plain"), "EMPLOYEE_FILE_TYPE"),
            (lambda: read(b"PK\x03\x04not a zip"), "EMPLOYEE_FILE_INVALID"),
            (lambda: read(workbook([["E-1"]], formula="B2")), "EMPLOYEE_FILE_FORMULAS"),
            (lambda: read(with_entry(good, "xl/vbaProject.bin")), "EMPLOYEE_FILE_MACROS"),
            (lambda: read(with_entry(good, "xl/externalLinks/externalLink1.xml")), "EMPLOYEE_FILE_EXTERNAL_LINKS"),
            (lambda: read(workbook([["E-1", "A", "a@mcciapune.in", "M", "9876543210", "3", "extra"]])), "EMPLOYEE_FILE_COLUMNS"),
            (lambda: read(workbook([[f"E-{n}", "A", "a@b.in", "M", "9876543210", "3"] for n in range(101)])), "EMPLOYEE_FILE_TOO_MANY_ROWS"),
            (lambda: read(workbook([], header=[None])), "EMPLOYEE_FILE_EMPTY"),
        )
        for call, code in cases:
            with self.subTest(code=code), self.assertRaisesRegex(DomainError, code):
                call()

    def test_service_checks_the_administrator_and_saves_nothing(self):
        transaction = Mock()
        database = Mock()
        database.transaction.return_value.__enter__ = Mock(return_value=transaction)
        database.transaction.return_value.__exit__ = Mock(return_value=False)
        service = EmployeeService(database, Mock())
        data = workbook([["E-1", "A", "a@mcciapune.in", "M", "9876543210", "3"]])
        with patch("meal_management.employees.require_actor") as actor:
            result = service.read_import_file("context", data, XLSX, "staff.xlsx")
        actor.assert_called_once_with(transaction, "context", {"ADMIN"})
        self.assertEqual(result["rows"][1][0], "E-1")
        transaction.execute.assert_not_called()
        transaction.insert.assert_not_called()


class EmployeeImportFileApiTests(ApiTestCase):
    def test_raw_excel_file_is_passed_to_the_reader(self):
        self.harness.login()
        self.harness.services.employees.read_import_file.return_value = {"rows": [HEADERS]}
        data = workbook([])
        response = self.client.post("/api/employees/import-file?filename=staff.xlsx", content=data,
                                    headers={**self.harness.headers(), "Content-Type": XLSX})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"rows": [HEADERS]})
        args = self.harness.services.employees.read_import_file.call_args.args
        self.assertEqual((args[1], args[2], args[3]), (data, XLSX, "staff.xlsx"))

    def test_admin_only_and_one_megabyte_limit(self):
        self.harness.login()
        response = self.client.post("/api/employees/import-file?filename=a.xlsx", content=b"x" * 1_048_577,
                                    headers={**self.harness.headers(), "Content-Type": XLSX})
        self.assertEqual(response.status_code, 413)
        self.harness.login("waiter")
        self.assert_error(self.client.post("/api/employees/import-file?filename=a.xlsx", content=b"PK",
                                           headers={**self.harness.headers(), "Content-Type": XLSX}), 403, "ROLE_REQUIRED")
        self.harness.services.employees.read_import_file.assert_not_called()


if __name__ == "__main__":
    unittest.main()
