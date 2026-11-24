from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import secrets
import threading
import unittest
from uuid import uuid4

import mysql.connector

from meal_management.config import Settings
from meal_management.database import Database
from meal_management.errors import DomainError
from meal_management.migrations import MigrationRunner
from mysql_support import IsolatedMySQLTestCase, MYSQL_TESTS_ENABLED, admin_connection_options
from test_mysql_services import MealServicesFixture


class RolesAndDepartmentMySQLTests(MealServicesFixture):
    def new_super_admin(self):
        password = secrets.token_urlsafe(24)
        email = f"super-{uuid4().hex[:10]}@example.test"
        staff_id = self.staff.create_staff(self.admin_context, "Extra Super Admin", email, password, {"SUPER_ADMIN"})
        context = self.staff.authenticate(email, password)
        return staff_id, email, password, context

    def new_admin(self, department_id=None):
        password = secrets.token_urlsafe(24)
        email = f"admin-{uuid4().hex[:10]}@example.test"
        staff_id = self.staff.create_staff(self.admin_context, "Standard Admin", email, password, {"ADMIN"}, department_id=department_id)
        context = self.staff.authenticate(email, password)
        return staff_id, email, password, context

    def test_migration_017_real_shape_and_backfill(self):
        if self.restricted:
            return
        db_name = f"{self.settings.db_name}_m17_{uuid4().hex[:8]}"
        cursor = self.admin_connection.cursor()
        try:
            cursor.execute(f"CREATE DATABASE `{db_name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci")
        finally:
            cursor.close()
        self.addCleanup(self._drop_test_db, db_name)

        target_settings = replace(self.admin_settings, db_name=db_name)
        target_db = Database(target_settings)
        migrations_dir = Path(__file__).resolve().parents[2] / "database" / "migrations"

        runner_early = MigrationRunner(target_db, migrations_dir, through=16)
        applied_early = runner_early.apply()
        self.assertEqual(len(applied_early), 16)

        with target_db.transaction() as tx:
            scanner_row = tx.one("SELECT id, email, is_scanner FROM staff_accounts WHERE email = 'meal-scanner@system.invalid'")
            self.assertIsNotNone(scanner_row)
            self.assertEqual(scanner_row["is_scanner"], 1)

            staff_2_id = tx.insert(
                "INSERT INTO staff_accounts (display_name, email, password_hash, is_scanner, is_active) "
                "VALUES (%s, %s, %s, 0, 1)",
                ("MCCIA Admin", "mcciaexplore@gmail.com", "hash_pw_2"),
            )
            tx.execute("INSERT INTO staff_account_roles (staff_id, role_code) VALUES (%s, 'ADMIN')", (staff_2_id,))

            staff_3_id = tx.insert(
                "INSERT INTO staff_accounts (display_name, email, password_hash, is_scanner, is_active) "
                "VALUES (%s, %s, %s, 0, 1)",
                ("Amol Joshi", "hr@autoclusterpune.org", "hash_pw_3"),
            )
            tx.execute("INSERT INTO staff_account_roles (staff_id, role_code) VALUES (%s, 'ADMIN')", (staff_3_id,))

            staff_4_id = tx.insert(
                "INSERT INTO staff_accounts (display_name, email, password_hash, is_scanner, is_active) "
                "VALUES (%s, %s, %s, 0, 1)",
                ("Operations Admin", "ops-admin@example.test", "hash_pw_4"),
            )
            tx.execute("INSERT INTO staff_account_roles (staff_id, role_code) VALUES (%s, 'ADMIN')", (staff_4_id,))

        runner_17 = MigrationRunner(target_db, migrations_dir)
        applied_17 = runner_17.apply()
        self.assertEqual(applied_17, (17,))

        with target_db.transaction() as tx:
            accounts = tx.all(
                "SELECT s.id, s.display_name, s.email, s.is_scanner, s.department_id, r.role_code "
                "FROM staff_accounts s JOIN staff_account_roles r ON r.staff_id = s.id "
                "ORDER BY s.id"
            )
            by_email = {row["email"]: row for row in accounts}

            self.assertIn("meal-scanner@system.invalid", by_email)
            self.assertEqual(by_email["meal-scanner@system.invalid"]["role_code"], "WAITER")
            self.assertEqual(by_email["meal-scanner@system.invalid"]["is_scanner"], 1)

            self.assertIn("mcciaexplore@gmail.com", by_email)
            self.assertEqual(by_email["mcciaexplore@gmail.com"]["role_code"], "SUPER_ADMIN")
            self.assertEqual(by_email["mcciaexplore@gmail.com"]["is_scanner"], 0)

            self.assertIn("hr@autoclusterpune.org", by_email)
            self.assertEqual(by_email["hr@autoclusterpune.org"]["role_code"], "SUPER_ADMIN")
            self.assertEqual(by_email["hr@autoclusterpune.org"]["is_scanner"], 0)

            self.assertIn("ops-admin@example.test", by_email)
            self.assertEqual(by_email["ops-admin@example.test"]["role_code"], "ADMIN")
            self.assertEqual(by_email["ops-admin@example.test"]["is_scanner"], 0)

            cols = {row["COLUMN_NAME"] for row in tx.all(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'staff_accounts'",
                (db_name,),
            )}
            self.assertIn("department_id", cols)

    def test_migration_017_refuses_without_a_named_active_admin(self):
        if self.restricted:
            return
        db_name = f"{self.settings.db_name}_m17r_{uuid4().hex[:8]}"
        cursor = self.admin_connection.cursor()
        try:
            cursor.execute(f"CREATE DATABASE `{db_name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci")
        finally:
            cursor.close()
        self.addCleanup(self._drop_test_db, db_name)
        target_db = Database(replace(self.admin_settings, db_name=db_name))
        migrations_dir = Path(__file__).resolve().parents[2] / "database" / "migrations"
        MigrationRunner(target_db, migrations_dir, through=16).apply()
        with target_db.transaction() as tx:
            other = tx.insert(
                "INSERT INTO staff_accounts (display_name, email, password_hash) VALUES (%s, %s, %s)",
                ("Fixture Admin", "fixture-only@example.test", "x"),
            )
            tx.execute("INSERT INTO staff_account_roles (staff_id, role_code) VALUES (%s, 'ADMIN')", (other,))
            inactive = tx.insert(
                "INSERT INTO staff_accounts (display_name, email, password_hash, is_active) VALUES (%s, %s, %s, 0)",
                ("Amol Joshi", "hr@autoclusterpune.org", "x"),
            )
            tx.execute("INSERT INTO staff_account_roles (staff_id, role_code) VALUES (%s, 'ADMIN')", (inactive,))
        with self.assertRaises(Exception) as caught:
            MigrationRunner(target_db, migrations_dir).apply()
        self.assertIn("migration_017_requires_active_super_admin", str(caught.exception))
        with target_db.transaction() as tx:
            self.assertIsNone(tx.one(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() "
                "AND TABLE_NAME = 'staff_accounts' AND COLUMN_NAME = 'department_id'"
            ))
            self.assertIsNone(tx.one("SELECT code FROM roles WHERE code = 'SUPER_ADMIN'"))
            self.assertEqual(tx.one("SELECT status FROM schema_migrations WHERE version = 17")["status"], "APPLYING")
            self.assertEqual(
                {row["role_code"] for row in tx.all("SELECT role_code FROM staff_account_roles")}, {"WAITER", "ADMIN"},
            )

    def _drop_test_db(self, db_name):
        cursor = self.admin_connection.cursor()
        try:
            cursor.execute(f"DROP DATABASE IF EXISTS `{db_name}`")
        finally:
            cursor.close()

    def test_email_approval_trigger_accepts_both_roles(self):
        super_id, _, _, _ = self.new_super_admin()
        admin_id, _, _, _ = self.new_admin()

        registration1, _ = self.employee()
        registration2, _ = self.employee()
        registration3, _ = self.employee()

        email_1 = registration1.email_id
        email_2 = registration2.email_id
        email_3 = registration3.email_id

        with self.db.transaction() as tx:
            tx.execute(
                "UPDATE email_queue SET approved_by_staff_id = %s, approved_at = UTC_TIMESTAMP(6), status = 'QUEUED' WHERE id = %s",
                (super_id, email_1),
            )
        row1 = self.rows("SELECT approved_by_staff_id, status FROM email_queue WHERE id = %s", (email_1,))[0]
        self.assertEqual(row1["approved_by_staff_id"], super_id)
        self.assertEqual(row1["status"], "QUEUED")

        with self.db.transaction() as tx:
            tx.execute(
                "UPDATE email_queue SET approved_by_staff_id = %s, approved_at = UTC_TIMESTAMP(6), status = 'QUEUED' WHERE id = %s",
                (admin_id, email_2),
            )
        row2 = self.rows("SELECT approved_by_staff_id, status FROM email_queue WHERE id = %s", (email_2,))[0]
        self.assertEqual(row2["approved_by_staff_id"], admin_id)
        self.assertEqual(row2["status"], "QUEUED")

        with self.assertRaises(mysql.connector.Error) as ctx, self.db.transaction() as tx:
            tx.execute(
                "UPDATE email_queue SET approved_by_staff_id = %s, approved_at = UTC_TIMESTAMP(6), status = 'QUEUED' WHERE id = %s",
                (self.waiter_id, email_3),
            )
        self.assertIn("Email approval requires an administrator", str(ctx.exception))

    def test_last_super_admin_cannot_be_demoted_or_deactivated(self):
        super_count = self.scalar(
            "SELECT COUNT(*) FROM staff_accounts s JOIN staff_account_roles r ON r.staff_id = s.id "
            "WHERE s.is_active = 1 AND s.is_scanner = 0 AND r.role_code = 'SUPER_ADMIN'"
        )
        if super_count > 1:
            other_supers = [
                row["id"] for row in self.rows(
                    "SELECT s.id FROM staff_accounts s JOIN staff_account_roles r ON r.staff_id = s.id "
                    "WHERE s.is_active = 1 AND s.is_scanner = 0 AND r.role_code = 'SUPER_ADMIN' AND s.id <> %s",
                    (self.admin_id,),
                )
            ]
            for other_id in other_supers:
                self.staff.set_active(self.admin_context, other_id, False)

        with self.assertRaisesRegex(DomainError, "LAST_ACTIVE_SUPER_ADMIN"):
            self.staff.set_active(self.admin_context, self.admin_id, False)

        with self.assertRaisesRegex(DomainError, "LAST_ACTIVE_SUPER_ADMIN"):
            self.staff.set_roles(self.admin_context, self.admin_id, ["ADMIN"])

        with self.assertRaisesRegex(DomainError, "LAST_ACTIVE_SUPER_ADMIN"):
            self.staff.set_roles(self.admin_context, self.admin_id, ["WAITER"])

        second_id, _, _, second_context = self.new_super_admin()
        self.staff.set_active(self.admin_context, second_id, False)
        self.staff.set_active(self.admin_context, second_id, True)
        self.staff.set_roles(self.admin_context, second_id, ["ADMIN"])


    def test_staff_department_management(self):
        admin_id, _, _, _ = self.new_admin(department_id=self.department_id)
        row = self.rows("SELECT department_id FROM staff_accounts WHERE id = %s", (admin_id,))[0]
        self.assertEqual(row["department_id"], self.department_id)

        self.staff.update(self.admin_context, admin_id, department_id=None)
        row_cleared = self.rows("SELECT department_id FROM staff_accounts WHERE id = %s", (admin_id,))[0]
        self.assertIsNone(row_cleared["department_id"])

        self.staff.update(self.admin_context, admin_id, department_id=self.department_id)
        row_restored = self.rows("SELECT department_id FROM staff_accounts WHERE id = %s", (admin_id,))[0]
        self.assertEqual(row_restored["department_id"], self.department_id)

        with self.assertRaisesRegex(DomainError, "DEPARTMENT_UNAVAILABLE"):
            self.staff.update(self.admin_context, admin_id, department_id=999999)


class LastSuperAdminRaceMySQLTests(MealServicesFixture):
    def test_two_super_admins_deactivating_each_other_leave_one(self):
        password = secrets.token_urlsafe(24)
        email = f"super-{uuid4().hex[:10]}@example.test"
        third_id = self.staff.create_staff(self.admin_context, "Racing Super Admin", email, password, {"SUPER_ADMIN"})
        third_context = self.staff.authenticate(email, password)
        barrier = threading.Barrier(2)

        def deactivate(ctx, target_id):
            try:
                barrier.wait(timeout=5)
                self.staff.set_active(ctx, target_id, False)
                return "DONE"
            except DomainError as err:
                return err.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(deactivate, self.admin_context, third_id)
            f2 = pool.submit(deactivate, third_context, self.admin_id)
            res = sorted([f1.result(), f2.result()])

        self.assertIn("DONE", res)

        remaining_supers = self.scalar(
            "SELECT COUNT(*) FROM staff_accounts s JOIN staff_account_roles r ON r.staff_id = s.id "
            "WHERE s.is_active = 1 AND s.is_scanner = 0 AND r.role_code = 'SUPER_ADMIN'"
        )
        self.assertEqual(remaining_supers, 1)
