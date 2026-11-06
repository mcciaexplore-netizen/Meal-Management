import os
import tempfile
import unittest
import zipfile
from datetime import date, datetime
from unittest.mock import Mock

from meal_management.errors import DomainError
from meal_management.meal_export import XLSX_TYPE, report_filename, report_range, text_cell, value_cell
from test_api import ApiTestCase


class ReportRangeTests(unittest.TestCase):
    def test_days_are_india_calendar_days(self):
        self.assertEqual(report_range(date(2026, 11, 1), date(2026, 11, 5)),
                         (datetime(2026, 10, 31, 18, 30), datetime(2026, 11, 5, 18, 30)))
        self.assertEqual(report_range(date(2026, 11, 5), date(2026, 11, 5)),
                         (datetime(2026, 11, 4, 18, 30), datetime(2026, 11, 5, 18, 30)))

    def test_bad_and_long_ranges_are_refused(self):
        self.assertEqual(report_range(date(2025, 11, 6), date(2026, 11, 6))[0], datetime(2025, 11, 5, 18, 30))
        for start, end, code in ((date(2025, 11, 5), date(2026, 11, 6), "REPORT_RANGE_TOO_LONG"),
                                 (date(2026, 11, 6), date(2026, 11, 5), "INVALID_DATE_RANGE"),
                                 (datetime(2026, 11, 1), date(2026, 11, 5), "INVALID_DATE_RANGE"),
                                 ("2026-11-01", date(2026, 11, 5), "INVALID_DATE_RANGE")):
            with self.subTest(code=code, start=start), self.assertRaisesRegex(DomainError, code):
                report_range(start, end)

    def test_file_name(self):
        self.assertEqual(report_filename(date(2026, 11, 1), date(2026, 11, 5)), "meal-report_2026-11-01_to_2026-11-05.xlsx")


class SafeCellTests(unittest.TestCase):
    def test_risky_text_phones_and_codes_are_stored_as_text_never_formulas(self):
        from openpyxl import Workbook, load_workbook

        book = Workbook(write_only=True)
        sheet = book.create_sheet("Meals")
        values = ['=HYPERLINK("http://x")', "+91 98765 43210", "-2+3", "@SUM(A1)", "\tTab", "\rReturn", "00123", "Plain", None]
        sheet.append([text_cell(sheet, value) for value in values])
        sheet.append([value_cell(sheet, 42), value_cell(sheet, date(2026, 11, 5), "dd mmm yyyy"), value_cell(sheet, "=1+1")])
        handle, path = tempfile.mkstemp(suffix=".xlsx")
        os.close(handle)
        self.addCleanup(os.unlink, path)
        book.save(path)
        with zipfile.ZipFile(path) as archive:
            xml = archive.read("xl/worksheets/sheet1.xml").decode()
        self.assertNotIn("<f>", xml)
        self.assertNotIn("<f ", xml)
        first, second = [[cell.value for cell in row] for row in load_workbook(path).active.iter_rows()]
        self.assertEqual(first[:8], ['=HYPERLINK("http://x")', "+91 98765 43210", "-2+3", "@SUM(A1)", "\tTab", "\nReturn", "00123", "Plain"])
        self.assertEqual(second[0], 42)
        self.assertEqual(second[1], datetime(2026, 11, 5))
        self.assertEqual(second[2], "=1+1")


class ExportApiTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.export = Mock()
        self.harness.app.state.meal_export = self.export
        handle, self.path = tempfile.mkstemp(suffix=".xlsx")
        os.write(handle, b"PK\x03\x04 fake workbook")
        os.close(handle)
        self.export.export.return_value = {"path": self.path, "filename": "meal-report_2026-11-01_to_2026-11-05.xlsx",
                                           "meal_rows": 3, "rejected_rows": 1}

    def tearDown(self):
        if os.path.exists(self.path):
            os.unlink(self.path)
        super().tearDown()

    def url(self, extra=""):
        return "/api/reports/meals/export?from=2026-11-01&through=2026-11-05" + extra

    def test_admin_gets_the_file_with_filters_passed_through_and_the_temp_file_removed(self):
        self.harness.login()
        response = self.client.get(self.url("&employee_id=7&department_id=3&meal_type_id=2"))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["content-type"], XLSX_TYPE)
        self.assertIn('filename="meal-report_2026-11-01_to_2026-11-05.xlsx"', response.headers["content-disposition"])
        self.assertTrue(response.headers["content-disposition"].startswith("attachment"))
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.content, b"PK\x03\x04 fake workbook")
        args, kwargs = self.export.export.call_args
        self.assertEqual(args[1:], (date(2026, 11, 1), date(2026, 11, 5)))
        self.assertEqual(kwargs, {"employee_id": 7, "department_id": 3, "meal_type_id": 2})
        self.assertFalse(os.path.exists(self.path))

    def test_errors_are_json_with_clear_messages(self):
        self.harness.login()
        for code, status, words in (("REPORT_TOO_LARGE", 413, "Narrow the dates"), ("REPORT_EMPTY", 404, "No meals match"),
                                    ("REPORT_EXPORT_RUNNING", 409, "already being prepared"),
                                    ("REPORT_RANGE_TOO_LONG", 400, "366 days")):
            with self.subTest(code=code):
                self.export.export.side_effect = DomainError(code)
                response = self.client.get(self.url())
                self.assert_error(response, status, code)
                self.assertIn(words, response.text)

    def test_inputs_and_role_are_checked(self):
        self.harness.login()
        for url in ("/api/reports/meals/export?through=2026-11-05", "/api/reports/meals/export?from=2026-11-01",
                    "/api/reports/meals/export?from=05-11-2026&through=2026-11-05", self.url("&employee_id=0")):
            with self.subTest(url=url):
                self.assert_error(self.client.get(url), 422, "INVALID_INPUT")
        self.harness.login("waiter")
        self.assert_error(self.client.get(self.url()), 403, "ROLE_REQUIRED")
        self.export.export.assert_not_called()


if __name__ == "__main__":
    unittest.main()
