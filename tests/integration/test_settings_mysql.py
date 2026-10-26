import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from test_mysql_services import MealServicesFixture


class SettingsMySQLTests(MealServicesFixture):
    def setUp(self):
        from meal_management.catalog import CatalogService

        super().setUp()
        self.catalog = CatalogService(self.db)

    def audit_actions(self, entity_type, entity_id):
        return [row["action"] for row in self.rows(
            "SELECT action FROM audit_events WHERE entity_type = %s AND entity_id = %s ORDER BY id",
            (entity_type, str(entity_id)),
        )]

    def shared(self):
        return self.rows(
            "SELECT p.scanner_id, p.meal_type_id, d.location_id FROM scan_app_settings p "
            "JOIN scanner_devices d ON d.id = p.scanner_id WHERE p.id = 1"
        )[0]

    def admin_statement_errno(self, sql, params=()):
        cursor = self.admin_connection.cursor()
        try:
            cursor.execute(sql, params)
        except Exception as error:
            return getattr(error, "errno", None)
        finally:
            cursor.close()
        return None

    def new_admin(self):
        password = secrets.token_urlsafe(24)
        email = f"admin-{uuid4().hex[:10]}@example.test"
        staff_id = self.staff.create_staff(self.admin_context, "Extra Admin", email, password, {"ADMIN"})
        return staff_id, email, password

    def test_department_edit_confirmed_deactivation_and_reactivation_keep_employees(self):
        from meal_management.errors import DomainError

        department = self.employees.create_department(self.admin_context, "Settings Department " + uuid4().hex[:6])
        suffix = uuid4().hex[:10]
        registration = self.employees.register(
            self.admin_context, f"SET-{suffix}", "Settings Employee", f"settings-{suffix}@example.test", department,
            company_name="Example Company", phone="+1 202 555 0101",
        )
        renamed = "Renamed Department " + uuid4().hex[:6]
        self.catalog.update_department(self.admin_context, department, renamed)
        self.assertEqual(self.catalog.department_usage(self.admin_context, department)["active_employees"], 1)
        with self.assertRaisesRegex(DomainError, "DEPARTMENT_HAS_ACTIVE_EMPLOYEES"):
            self.catalog.set_department_active(self.admin_context, department, False)
        self.assertEqual(self.scalar("SELECT is_active FROM departments WHERE id = %s", (department,)), 1)
        self.catalog.set_department_active(self.admin_context, department, False, confirm=True)
        self.employees.update(self.admin_context, registration.employee_id, full_name="Still Editable", department_id=department)
        self.assertEqual(self.scalar("SELECT full_name FROM employees WHERE id = %s", (registration.employee_id,)), "Still Editable")
        other = self.employees.create_department(self.admin_context, "Other Department " + uuid4().hex[:6])
        self.employees.update(self.admin_context, registration.employee_id, department_id=other)
        with self.assertRaisesRegex(DomainError, "DEPARTMENT_UNAVAILABLE"):
            self.employees.update(self.admin_context, registration.employee_id, department_id=department)
        with self.assertRaisesRegex(DomainError, "DEPARTMENT_UNAVAILABLE"):
            self.employees.register(self.admin_context, f"SET2-{suffix}", "New Employee", f"new-{suffix}@example.test", department,
                                    company_name="Example Company", phone="+1 202 555 0101")
        self.catalog.set_department_active(self.admin_context, department, True)
        self.assertEqual(self.audit_actions("departments", department), [
            "DEPARTMENT_CREATED", "DEPARTMENT_UPDATED", "DEPARTMENT_STATUS_CHANGED", "DEPARTMENT_STATUS_CHANGED",
        ])
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM departments WHERE id = %s", (department,)), 1)

    def test_codes_and_names_already_in_use_are_refused(self):
        from meal_management.errors import DomainError

        first = self.catalog.create_location(self.admin_context, "LOC-" + uuid4().hex[:6], "First")
        second_code = "LOC-" + uuid4().hex[:6]
        self.catalog.create_location(self.admin_context, second_code, "Second")
        with self.assertRaisesRegex(DomainError, "LOCATION_CODE_EXISTS"):
            self.catalog.update_location(self.admin_context, first, second_code, "First")
        counter = self.catalog.create_scanner(self.admin_context, "CTR-" + uuid4().hex[:6], "Counter", first)
        with self.assertRaisesRegex(DomainError, "SCANNER_CODE_EXISTS"):
            self.catalog.update_scanner(self.admin_context, counter, "TEST-SCANNER", "Counter")
        meal = self.catalog.create_meal_type(self.admin_context, "MEAL-" + uuid4().hex[:6], "Supper")
        with self.assertRaisesRegex(DomainError, "MEAL_TYPE_CODE_EXISTS"):
            self.catalog.update_meal_type(self.admin_context, meal, "TEST-LUNCH", "Supper")
        names = [row["name"] for row in self.rows("SELECT name FROM departments")]
        target = self.employees.create_department(self.admin_context, "Unique " + uuid4().hex[:6])
        with self.assertRaisesRegex(DomainError, "DEPARTMENT_NAME_EXISTS"):
            self.catalog.update_department(self.admin_context, target, names[0])

    def test_inactive_meal_type_counter_and_location_refuse_scans_until_reactivated(self):
        from meal_management.errors import DomainError

        _, token = self.employee()
        for deactivate, reactivate, code in (
            (lambda: self.catalog.set_meal_type_active(self.admin_context, self.meal_type_id, False),
             lambda: self.catalog.set_meal_type_active(self.admin_context, self.meal_type_id, True), "MEAL_TYPE_UNAVAILABLE"),
            (lambda: self.catalog.set_scanner_active(self.admin_context, self.scanner_id, False),
             lambda: self.catalog.set_scanner_active(self.admin_context, self.scanner_id, True), "SCANNER_INACTIVE"),
            (lambda: self.catalog.set_location_active(self.admin_context, self.location_id, False),
             lambda: self.catalog.set_location_active(self.admin_context, self.location_id, True), "LOCATION_INACTIVE"),
        ):
            deactivate()
            try:
                result = self.meals.record(self.waiter_context, self.scan(token))
                self.assertFalse(result.approved)
                self.assertEqual(result.code, code)
            except DomainError as error:
                self.assertEqual(error.code, code)
            finally:
                reactivate()
        self.assertTrue(self.meals.record(self.waiter_context, self.scan(token)).approved)

    def test_shared_scanner_rows_are_protected_by_service_and_database(self):
        from meal_management.errors import DomainError

        shared = self.shared()
        self.catalog.update_scanner(self.admin_context, shared["scanner_id"], "SHARED-SCANNER", "Front counter")
        self.assertEqual(self.scalar("SELECT name FROM scanner_devices WHERE id = %s", (shared["scanner_id"],)), "Front counter")
        for call in (
            lambda: self.catalog.set_scanner_active(self.admin_context, shared["scanner_id"], False),
            lambda: self.catalog.set_location_active(self.admin_context, shared["location_id"], False),
            lambda: self.catalog.update_scanner(self.admin_context, shared["scanner_id"], "RENAMED", "Front counter"),
            lambda: self.catalog.update_location(self.admin_context, shared["location_id"], "RENAMED", "Meal Scanner"),
        ):
            with self.assertRaisesRegex(DomainError, "SHARED_SCANNER_SETTING_PROTECTED"):
                call()
        for sql, identifier in (
            ("UPDATE scanner_devices SET is_active = 0 WHERE id = %s", shared["scanner_id"]),
            ("UPDATE scanner_devices SET code = 'X' WHERE id = %s", shared["scanner_id"]),
            ("UPDATE scanner_devices SET location_id = %s WHERE id = %s", None),
            ("UPDATE locations SET is_active = 0 WHERE id = %s", shared["location_id"]),
            ("UPDATE staff_accounts SET is_active = 0 WHERE is_scanner = 1 AND id > %s", 0),
            ("UPDATE staff_accounts SET email = 'x@example.test' WHERE is_scanner = 1 AND id > %s", 0),
        ):
            params = (self.location_id, shared["scanner_id"]) if identifier is None else (identifier,)
            with self.subTest(sql=sql):
                self.assertEqual(self.admin_statement_errno(sql, params), 1644)
        self.assertIsNone(self.admin_statement_errno("UPDATE scanner_devices SET name = name WHERE id = %s", (shared["scanner_id"],)))

    def test_staff_edit_ends_sessions_on_email_change_and_protects_scanner_identity(self):
        from meal_management.errors import DomainError
        from meal_management.models import ServerContext

        password = secrets.token_urlsafe(24)
        staff_id = self.staff.create_staff(self.admin_context, "Editable Waiter", f"edit-{uuid4().hex[:8]}@example.test", password, {"WAITER"})
        email = self.scalar("SELECT email FROM staff_accounts WHERE id = %s", (staff_id,))
        session = self.staff.authenticate(email, password)
        self.assertEqual(self.staff.update(self.admin_context, staff_id, "Renamed Waiter", email)["sessions_ended"], 0)
        self.staff.current(ServerContext(session.session_token))
        new_email = f"moved-{uuid4().hex[:8]}@example.test"
        self.assertEqual(self.staff.update(self.admin_context, staff_id, "Renamed Waiter", new_email.upper())["sessions_ended"], 1)
        with self.assertRaises(DomainError):
            self.staff.current(ServerContext(session.session_token))
        with self.assertRaises(DomainError):
            self.staff.authenticate(email, password)
        self.staff.authenticate(new_email, password)
        with self.assertRaisesRegex(DomainError, "STAFF_EMAIL_EXISTS"):
            self.staff.update(self.admin_context, staff_id, "Renamed Waiter", "admin@example.test")
        scanner_id = self.scalar("SELECT id FROM staff_accounts WHERE is_scanner = 1")
        for call in (lambda: self.staff.update(self.admin_context, scanner_id, "X", "x@example.test"),
                     lambda: self.staff.set_active(self.admin_context, scanner_id, False),
                     lambda: self.staff.set_roles(self.admin_context, scanner_id, ["ADMIN"])):
            with self.assertRaises(DomainError):
                call()
        actions = self.audit_actions("staff_accounts", staff_id)
        self.assertEqual(actions.count("STAFF_UPDATED"), 2)

    def test_role_changes_work_with_delete_on_role_rows_and_keep_one_admin(self):
        from meal_management.errors import DomainError

        staff_id = self.staff.create_staff(self.admin_context, "Role Person", f"role-{uuid4().hex[:8]}@example.test", secrets.token_urlsafe(24), {"WAITER"})
        self.staff.set_roles(self.admin_context, staff_id, ["ADMIN", "WAITER"])
        self.assertEqual({row["role_code"] for row in self.rows("SELECT role_code FROM staff_account_roles WHERE staff_id = %s", (staff_id,))}, {"ADMIN", "WAITER"})
        self.staff.set_roles(self.admin_context, staff_id, ["WAITER"])
        self.assertEqual([row["role_code"] for row in self.rows("SELECT role_code FROM staff_account_roles WHERE staff_id = %s", (staff_id,))], ["WAITER"])
        self.assertIn("STAFF_ROLES_CHANGED", self.audit_actions("staff_accounts", staff_id))
        admins = self.scalar("SELECT COUNT(*) FROM staff_accounts s JOIN staff_account_roles r ON r.staff_id = s.id WHERE s.is_active = 1 AND s.is_scanner = 0 AND r.role_code = 'ADMIN'")
        if admins == 1:
            with self.assertRaisesRegex(DomainError, "LAST_ACTIVE_ADMIN"):
                self.staff.set_roles(self.admin_context, self.admin_id, ["WAITER"])

    def test_settings_changes_use_only_runtime_grants(self):
        from meal_management import database
        from meal_management.runtime_account import DELETE_TABLES, INSERT_TABLES, UPDATE_TABLES

        seen = []
        originals = {name: getattr(database.Session, name) for name in ("one", "all", "execute", "insert")}

        def recorder(name):
            def wrapped(session, sql, params=()):
                seen.append(sql)
                return originals[name](session, sql, params)
            return wrapped

        from unittest.mock import patch

        with patch.multiple(database.Session, **{name: recorder(name) for name in originals}):
            department = self.employees.create_department(self.admin_context, "Grant Department " + uuid4().hex[:6])
            self.catalog.update_department(self.admin_context, department, "Grant Renamed " + uuid4().hex[:6])
            self.catalog.set_department_active(self.admin_context, department, False)
            meal = self.catalog.create_meal_type(self.admin_context, "GRANT-" + uuid4().hex[:6], "Grant Meal")
            self.catalog.update_meal_type(self.admin_context, meal, "GRANT-" + uuid4().hex[:6], "Grant Meal 2")
            self.catalog.set_meal_type_active(self.admin_context, meal, False)
            staff_id = self.staff.create_staff(self.admin_context, "Grant Staff", f"grant-{uuid4().hex[:8]}@example.test", secrets.token_urlsafe(24), {"WAITER"})
            self.staff.set_roles(self.admin_context, staff_id, ["ADMIN"])
            self.staff.update(self.admin_context, staff_id, "Grant Staff 2", f"grant2-{uuid4().hex[:8]}@example.test")
        for sql in seen:
            words = sql.split()
            verb = words[0].upper()
            if verb == "INSERT":
                self.assertIn(words[2], INSERT_TABLES, sql)
            elif verb == "UPDATE":
                self.assertIn(words[1], UPDATE_TABLES, sql)
            elif verb == "DELETE":
                self.assertIn(words[2], DELETE_TABLES, sql)


class LastAdminConcurrencyMySQLTests(MealServicesFixture):
    def new_admin(self):
        password = secrets.token_urlsafe(24)
        email = f"admin-{uuid4().hex[:10]}@example.test"
        staff_id = self.staff.create_staff(self.admin_context, "Extra Admin", email, password, {"ADMIN"})
        return staff_id, email, password

    def test_concurrent_deactivation_of_the_last_two_admins_leaves_one(self):
        from meal_management.errors import DomainError

        others = [row["id"] for row in self.rows(
            "SELECT s.id FROM staff_accounts s JOIN staff_account_roles r ON r.staff_id = s.id "
            "WHERE s.is_active = 1 AND s.is_scanner = 0 AND r.role_code = 'ADMIN' AND s.id <> %s", (self.admin_id,),
        )]
        for other in others:
            self.staff.set_active(self.admin_context, other, False)
        first, email, password = self.new_admin()
        first_context = self.staff.authenticate(email, password)
        barrier = threading.Barrier(2)

        def deactivate(context, target):
            barrier.wait(timeout=10)
            try:
                self.staff.set_active(context, target, False)
                return "DONE"
            except DomainError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = sorted(pool.map(lambda pair: deactivate(*pair), [(self.admin_context, first), (first_context, self.admin_id)]))
        remaining = self.scalar(
            "SELECT COUNT(*) FROM staff_accounts s JOIN staff_account_roles r ON r.staff_id = s.id "
            "WHERE s.is_active = 1 AND s.is_scanner = 0 AND r.role_code = 'ADMIN'"
        )
        self.assertEqual(remaining, 1)
        self.assertIn("DONE", results)
