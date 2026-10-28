from datetime import datetime, timedelta, timezone
from uuid import uuid4

from test_mysql_services import MealServicesFixture


class ImportDepartmentsMySQLTests(MealServicesFixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from meal_management.queries import QueryService

        cls.queries = QueryService(cls.db)

    def rows(self, statement, parameters=()):
        cursor = self.admin_connection.cursor(dictionary=True)
        try:
            cursor.execute(f"USE `{self.database_name}`")
            cursor.execute(statement, parameters)
            return cursor.fetchall()
        finally:
            cursor.close()

    def person(self, department=None, **extra):
        tag = uuid4().hex[:10]
        row = {"employee_code": f"IMP-{tag}", "full_name": f"Imported {tag}", "email": f"imp-{tag}@mcciapune.in",
               "company_name": "MCCIA", "phone": "+91 98765 43210", **extra}
        if department is not None:
            row["department"] = department
        return row

    def department(self, name):
        found = self.rows("SELECT id, name, is_active FROM departments WHERE name = %s", (name,))
        return found[0] if found else None

    def import_rows(self, rows, request_id=None):
        return self.employees.register_bulk(self.admin_context, request_id or uuid4(), rows)

    def test_employees_without_a_department_are_stored_listed_and_scanned(self):
        from meal_management.errors import DomainError

        result = self.import_rows([self.person(), self.person()])
        ids = [row["employee_id"] for row in result["employees"]]
        stored = self.rows(f"SELECT department_id FROM employees WHERE id IN ({ids[0]}, {ids[1]})")
        self.assertEqual([row["department_id"] for row in stored], [None, None])
        detail = self.queries.employee(self.admin_context, ids[0])
        self.assertEqual((detail["department_id"], detail["department_name"]), (None, None))
        listed = {row["id"]: row for row in self.queries.list_employees(self.admin_context, limit=100)["items"]}
        self.assertIn(ids[0], listed)
        self.assertIsNone(listed[ids[0]]["department_name"])
        token = self.qr.email_queue.preview(self.admin_context, result["employees"][0]["email_id"]).token
        self.meals.record(self.waiter_context, self.scan(token))
        now = datetime.now(timezone.utc)
        history = self.queries.meal_history(self.admin_context, now - timedelta(days=1), now + timedelta(days=1), limit=100)
        meal = next(row for row in history["items"] if row.get("employee_id") == ids[0])
        self.assertIsNone(meal["department_name"])
        filtered = self.queries.meal_history(self.admin_context, now - timedelta(days=1), now + timedelta(days=1), limit=100,
                                             department_id=self.department_id)
        self.assertNotIn(ids[0], [row.get("employee_id") for row in filtered["items"]])
        self.employees.update(self.admin_context, ids[0], department_id=self.department_id)
        self.assertEqual(self.queries.employee(self.admin_context, ids[0])["department_name"], "Integration Department")
        with self.assertRaises(DomainError):
            self.employees.register(self.admin_context, "SINGLE-" + uuid4().hex[:8], "Single", "single@mcciapune.in", None,
                                    company_name="MCCIA", phone="+91 98765 43210")

    def test_names_match_existing_departments_and_create_a_built_in_facility_once(self):
        before = self.rows("SELECT COUNT(*) AS n FROM departments")[0]["n"]
        self.assertIsNone(self.department("Auditorium"))
        first = self.import_rows([self.person("  integration   DEPARTMENT "), self.person("auditorium"), self.person("AUDITORIUM ")])
        auditorium = self.department("Auditorium")
        self.assertIsNotNone(auditorium)
        ids = [row["employee_id"] for row in first["employees"]]
        stored = self.rows(f"SELECT id, department_id FROM employees WHERE id IN ({','.join(map(str, ids))}) ORDER BY id")
        self.assertEqual([row["department_id"] for row in stored], [self.department_id, auditorium["id"], auditorium["id"]])
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM departments")[0]["n"], before + 1)
        created = self.rows("SELECT actor_staff_id, after_data FROM audit_events WHERE action = 'DEPARTMENT_CREATED' AND entity_id = %s",
                            (str(auditorium["id"]),))
        self.assertEqual(len(created), 1)
        self.import_rows([self.person("Auditorium")])
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM departments")[0]["n"], before + 1)

    def test_inactive_or_unknown_names_reject_the_whole_file_and_create_nothing(self):
        from meal_management.errors import DomainError

        retired = self.employees.create_department(self.admin_context, "AI Studio " + uuid4().hex[:6])
        name = self.rows("SELECT name FROM departments WHERE id = %s", (retired,))[0]["name"]
        self.rows("UPDATE departments SET is_active = 0 WHERE id = %s", (retired,))
        self.admin_connection.commit()
        employees_before = self.rows("SELECT COUNT(*) AS n FROM employees")[0]["n"]
        for rows, code in (
            ([self.person("Training & Seminar Hall"), self.person(name)], "DEPARTMENT_UNAVAILABLE"),
            ([self.person("Training & Seminar Hall"), self.person("Department 7")], "DEPARTMENT_NOT_FOUND"),
        ):
            with self.subTest(code=code), self.assertRaisesRegex(DomainError, code):
                self.import_rows(rows)
            self.assertIsNone(self.department("Training & Seminar Hall"))
            self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM employees")[0]["n"], employees_before)
        with self.assertRaisesRegex(DomainError, "INVALID_EMPLOYEE_BATCH"):
            self.import_rows([self.person(department_id=self.department_id)])

    def test_same_request_replays_without_creating_more_employees(self):
        request_id = uuid4()
        rows = [self.person("Exhibition Centre"), self.person()]
        first = self.import_rows(rows, request_id)
        count = self.rows("SELECT COUNT(*) AS n FROM employees")[0]["n"]
        again = self.import_rows(rows, request_id)
        self.assertTrue(again["replayed"])
        self.assertEqual(again["batch_id"], first["batch_id"])
        self.assertEqual(sorted(row["employee_id"] for row in again["employees"]), sorted(row["employee_id"] for row in first["employees"]))
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM employees")[0]["n"], count)

    def test_migration_016_leaves_the_column_nullable_with_its_foreign_key(self):
        column = self.rows("SELECT IS_NULLABLE FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() "
                           "AND TABLE_NAME = 'employees' AND COLUMN_NAME = 'department_id'")
        self.assertEqual(column[0]["IS_NULLABLE"], "YES")
        keys = self.rows("SELECT REFERENCED_TABLE_NAME FROM information_schema.KEY_COLUMN_USAGE WHERE TABLE_SCHEMA = DATABASE() "
                         "AND TABLE_NAME = 'employees' AND COLUMN_NAME = 'department_id' AND REFERENCED_TABLE_NAME IS NOT NULL")
        self.assertEqual([row["REFERENCED_TABLE_NAME"] for row in keys], ["departments"])
