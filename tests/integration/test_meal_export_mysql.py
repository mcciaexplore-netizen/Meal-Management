import os
import zipfile
from datetime import date, datetime, timedelta, timezone

from test_mysql_services import MealServicesFixture

INDIA = timezone(timedelta(hours=5, minutes=30))


class MealExportMySQLTests(MealServicesFixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from meal_management.meal_export import MealReportExport
        from meal_management.queries import QueryService

        cls.queries = QueryService(cls.db)
        cls.export = MealReportExport(cls.db)
        cls.today = datetime.now(INDIA).date()
        helper = cls.__new__(cls)
        cls.first_employee, token = helper.employee()
        cls.meals.record(cls.waiter_context, helper.scan(token))
        second, token = helper.employee()
        cls.second_employee = second
        cls.meals.record(cls.waiter_context, helper.scan(token))
        cls.meals.record(cls.waiter_context, helper.scan(token))
        with cls.db.transaction() as tx:
            tx.execute("UPDATE employees SET full_name = %s, phone = %s, employee_code = %s WHERE id = %s",
                       ('=HYPERLINK("http://evil")', "+91 98765 43210", "00123", second.employee_id))
        _, scan = helper.master_approval(quantity=3)
        cls.visitor = cls.meals.record(cls.waiter_context, scan)
        from meal_management.security import generate_token
        cls.meals.record(cls.waiter_context, helper.scan(generate_token()))
        cls.corrected_type = None
        with cls.db.transaction() as tx:
            cls.corrected_type = tx.insert("INSERT INTO meal_types (code, name) VALUES (%s, %s)", ("TEST-DINNER", "Integration Dinner"))
        cls.meals.correct_meal_type(cls.admin_context, cls.visitor.meal_ids[0], cls.corrected_type, "Wrong counter")

    def rows(self, statement, parameters=()):
        cursor = self.admin_connection.cursor(dictionary=True)
        try:
            cursor.execute(statement, parameters)
            return cursor.fetchall()
        finally:
            cursor.close()

    def run_export(self, **filters):
        result = self.export.export(self.admin_context, self.today - timedelta(days=1), self.today, **filters)
        self.addCleanup(lambda: os.path.exists(result["path"]) and os.unlink(result["path"]))
        return result

    def sheets(self, path):
        from openpyxl import load_workbook

        book = load_workbook(path, data_only=False)
        return {sheet.title: [[cell.value for cell in row] for row in sheet.iter_rows()] for sheet in book.worksheets}

    def screen(self, **filters):
        start = datetime.combine(self.today - timedelta(days=1), datetime.min.time(), INDIA)
        end = datetime.combine(self.today + timedelta(days=1), datetime.min.time(), INDIA)
        rows, before = [], None
        while True:
            page = self.queries.meal_history(self.admin_context, start, end, limit=2, before=before, **filters)
            rows += page["items"]
            before = page["next_cursor"]
            if before is None:
                return rows

    def test_rows_match_the_screen_for_every_filter_newest_first(self):
        department = self.rows("SELECT department_id FROM employees WHERE id = %s", (self.first_employee.employee_id,))[0]["department_id"]
        for filters in ({}, {"employee_id": self.second_employee.employee_id}, {"meal_type_id": self.corrected_type},
                        {"department_id": department}, {"employee_id": self.second_employee.employee_id, "meal_type_id": self.meal_type_id}):
            with self.subTest(filters=filters):
                expected = [row["meal_id"] for row in self.screen(**filters)]
                result = self.run_export(**filters)
                meals = self.sheets(result["path"])["Meals"]
                self.assertEqual([row[0] for row in meals[1:]], expected)
                self.assertEqual(result["meal_rows"], len(expected))

    def test_total_matches_the_screen_totals_and_summary_matches_the_sheet(self):
        from meal_management.reports import ReportService

        start = datetime.combine(self.today - timedelta(days=1), datetime.min.time(), INDIA)
        end = datetime.combine(self.today + timedelta(days=1), datetime.min.time(), INDIA)
        totals = ReportService(self.db).meal_report(self.admin_context, start, end)
        result = self.run_export()
        sheets = self.sheets(result["path"])
        self.assertEqual(list(sheets), ["Meals", "Summary", "Rejected scans"])
        self.assertEqual(result["meal_rows"], totals["totals"]["meal_count"])
        summary = {row[0]: row[1] for row in sheets["Summary"] if row and row[0]}
        meals = sheets["Meals"][1:]
        self.assertEqual(summary["Total meals"], len(meals))
        self.assertEqual(summary["Employee meals"], sum(1 for row in meals if row[8] == "Employee"))
        self.assertEqual(summary["Visitor meals"], sum(1 for row in meals if row[8] == "Visitor"))
        rows = sheets["Summary"]
        start_table = next(index for index, row in enumerate(rows) if row and row[0] == "Meals by employee")
        header = rows[start_table + 1]
        self.assertEqual((header[0], header[1], header[-1]), ("Name", "Employee code", "Total"))
        by_employee = {row[1]: row[-1] for row in rows[start_table + 2:] if row and row[0]}
        self.assertEqual(by_employee["00123"], 2)
        self.assertEqual(sum(by_employee.values()), summary["Employee meals"])
        audit = self.rows("SELECT actor_staff_id, after_data FROM audit_events WHERE action = 'MEAL_REPORT_EXPORTED' "
                          "AND entity_id = %s ORDER BY id DESC LIMIT 1", (result["filename"],))[0]
        self.assertEqual(audit["actor_staff_id"], self.admin_id)
        self.assertIn('"meal_rows": ' + str(len(meals)), audit["after_data"])

    def test_cells_are_safe_typed_and_corrections_shown(self):
        result = self.run_export()
        meals = self.sheets(result["path"])["Meals"]
        header = meals[0]
        self.assertEqual(header[:4], ["Meal no.", "Date", "Time (IST)", "Name"])
        risky = next(row for row in meals[1:] if row[4] == "00123")
        self.assertEqual((risky[3], risky[7]), ('=HYPERLINK("http://evil")', "+91 98765 43210"))
        self.assertIsInstance(risky[0], int)
        self.assertIsInstance(risky[1], datetime)
        self.assertRegex(risky[2], r"^\d\d:\d\d$")
        corrected = next(row for row in meals[1:] if row[0] == self.visitor.meal_ids[0])
        self.assertEqual((corrected[8], corrected[11], corrected[12], corrected[13], corrected[14]),
                         ("Visitor", "Integration Dinner", "Yes", "Integration Lunch", "Wrong counter"))
        with zipfile.ZipFile(result["path"]) as archive:
            xml = "".join(archive.read(name).decode() for name in archive.namelist() if name.startswith("xl/worksheets/"))
        self.assertNotIn("<f>", xml)
        self.assertNotIn("<f ", xml)
        self.assertIn('ySplit="1"', xml)
        self.assertIn("<autoFilter", xml)

    def test_rejected_scans_follow_the_dates_only(self):
        result = self.run_export(employee_id=self.second_employee.employee_id)
        rejected = self.sheets(result["path"])["Rejected scans"]
        self.assertEqual(rejected[0], ["Date", "Time (IST)", "Who", "Reason", "Waiter", "Location"])
        start = datetime.combine(self.today - timedelta(days=1), datetime.min.time(), INDIA).astimezone(timezone.utc).replace(tzinfo=None)
        expected = self.rows("SELECT COUNT(*) AS n FROM scan_attempts WHERE outcome = 'REJECTED' AND received_at >= %s", (start,))[0]["n"]
        self.assertEqual(len(rejected) - 1, expected)
        self.assertIn("Unknown QR", [row[2] for row in rejected[1:]])

    def test_empty_too_large_running_and_bad_ranges_send_no_file_and_no_audit(self):
        from meal_management.errors import DomainError
        from meal_management.meal_export import MealReportExport
        from meal_management.database import Database, Session

        before = self.rows("SELECT COUNT(*) AS n FROM audit_events WHERE action = 'MEAL_REPORT_EXPORTED'")[0]["n"]
        old = date(2001, 1, 1)
        with self.assertRaisesRegex(DomainError, "REPORT_EMPTY"):
            self.export.export(self.admin_context, old, old)
        with self.assertRaisesRegex(DomainError, "REPORT_TOO_LARGE"):
            MealReportExport(self.db, max_rows=2).export(self.admin_context, self.today - timedelta(days=1), self.today)
        with self.assertRaisesRegex(DomainError, "REPORT_RANGE_TOO_LONG"):
            self.export.export(self.admin_context, self.today - timedelta(days=366), self.today)
        with self.assertRaisesRegex(DomainError, "INVALID_DATE_RANGE"):
            self.export.export(self.admin_context, self.today, self.today - timedelta(days=1))
        with self.assertRaisesRegex(DomainError, "ROLE_REQUIRED"):
            self.export.export(self.waiter_context, self.today, self.today)
        holder = Database(self.db.settings)._connect()
        try:
            session = Session(holder)
            name = "meal_report_export:" + self.db.settings.db_name[:40]
            self.assertEqual(session.one("SELECT GET_LOCK(%s, 0) AS got", (name,))["got"], 1)
            with self.assertRaisesRegex(DomainError, "REPORT_EXPORT_RUNNING"):
                self.export.export(self.admin_context, self.today, self.today)
            session.one("SELECT RELEASE_LOCK(%s) AS released", (name,))
        finally:
            holder.close()
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM audit_events WHERE action = 'MEAL_REPORT_EXPORTED'")[0]["n"], before)
        self.run_export()

    def test_meals_recorded_after_the_count_are_left_out(self):
        from meal_management.meal_export import MealReportExport

        export = MealReportExport(self.db, page=1)
        original = export._pages
        state = {"added": False}

        def pages(*args):
            for row in original(*args):
                if not state["added"]:
                    state["added"] = True
                    _, token = self.employee()
                    self.meals.record(self.waiter_context, self.scan(token))
                yield row

        export._pages = pages
        result = export.export(self.admin_context, self.today - timedelta(days=1), self.today)
        self.addCleanup(os.unlink, result["path"])
        self.assertTrue(state["added"])
        self.assertEqual(len(self.sheets(result["path"])["Meals"]) - 1, result["meal_rows"])
