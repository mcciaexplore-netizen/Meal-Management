import secrets
import tempfile
import uuid
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from uuid import uuid4

from mysql_support import IsolatedMySQLTestCase

MIGRATIONS = Path(__file__).resolve().parents[2] / "database" / "migrations"


class FakeS3:
    def __init__(self, versioning=None, listing=None):
        self.deleted = []
        self.versioning = versioning
        self.listing = listing

    def delete_object(self, Bucket, Key):
        self.deleted.append((Bucket, Key))

    def get_bucket_versioning(self, Bucket):
        if self.versioning is None:
            raise PermissionError("AccessDenied")
        return {"Status": self.versioning}

    def get_paginator(self, name):
        if self.listing is None:
            raise PermissionError("AccessDenied")
        listing = self.listing

        class Pages:
            def paginate(self, Bucket):
                return [{"Contents": [{"Key": key} for key in listing]}]

        return Pages()


class TestDataResetMySQLTests(IsolatedMySQLTestCase):
    def setUp(self):
        from meal_management.database import Database
        from meal_management.migrations import MigrationRunner

        self.name = f"{self.database_name[:36]}_r{uuid.uuid4().hex[:8]}_test"
        self.execute(f"CREATE DATABASE `{self.name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci")
        self.addCleanup(self.execute, f"DROP DATABASE `{self.name}`")
        self.settings = replace(self.admin_settings, db_name=self.name)
        self.db = Database(self.settings)
        MigrationRunner(self.db, MIGRATIONS).apply()
        self.photo_root = tempfile.TemporaryDirectory()
        self.addCleanup(self.photo_root.cleanup)
        self.output_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.output_dir.cleanup)
        self.lines = []
        self.seed()

    def execute(self, sql, params=()):
        cursor = self.admin_connection.cursor()
        try:
            cursor.execute(sql, params)
        finally:
            cursor.close()

    def rows(self, sql):
        cursor = self.admin_connection.cursor()
        try:
            cursor.execute(sql)
            return cursor.fetchall()
        finally:
            cursor.close()

    def query(self, sql, params=()):
        cursor = self.admin_connection.cursor(dictionary=True)
        try:
            cursor.execute(f"USE `{self.name}`")
            cursor.execute("SET SESSION information_schema_stats_expiry = 0")
            cursor.execute(sql, params)
            return cursor.fetchall()
        finally:
            cursor.execute(f"USE `{self.database_name}`")
            cursor.close()

    def errno(self, sql):
        cursor = self.admin_connection.cursor()
        try:
            cursor.execute(f"USE `{self.name}`")
            cursor.execute(sql)
        except Exception as error:
            return getattr(error, "errno", None)
        finally:
            cursor.execute(f"USE `{self.database_name}`")
            cursor.close()
        return None

    def seed(self):
        from meal_management.accounts import StaffService
        from meal_management.employees import EmployeeService
        from meal_management.meals import MealService
        from meal_management.qr import QrService
        from meal_management.scan_app import ScanAppService
        from meal_management.scan_receipts import ScanReceiptService
        from meal_management.scanner_phones import ScannerPhoneService
        from meal_management.security import QrRenderer, TokenVault

        staff = StaffService(self.db)
        password = self.password = secrets.token_urlsafe(24)
        self.admin_id = staff.bootstrap_admin("Reset Admin", "reset-admin@example.test", password)
        self.admin = staff.authenticate("reset-admin@example.test", password)
        staff.create_staff(self.admin, "Reset Waiter", "reset-waiter@example.test", password, {"WAITER"})
        with self.assertRaises(Exception):
            staff.authenticate("reset-admin@example.test", "wrong-password-123")
        qr = QrService(self.db, TokenVault(self.settings.qr_encryption_keys), QrRenderer())
        employees = EmployeeService(self.db, qr)
        department = employees.create_department(self.admin, "Reset Department")
        self.types = {row["code"]: row["id"] for row in self.query("SELECT id, code FROM meal_types")}
        tokens, self.photo_keys = [], []
        for number in range(3):
            key = secrets.token_hex(32) + ".jpg"
            self.photo_keys.append(key)
            registration = employees.register(
                self.admin, f"RS-{number}", f"Reset Employee {number}", f"reset{number}@example.test", department,
                company_name="Example", phone="+91 98765 4321" + str(number), selfie_object_key=key,
            )
            tokens.append((registration, qr.email_queue.preview(self.admin, registration.email_id).token))
        self.execute(f"UPDATE `{self.name}`.employees SET selfie_object_key = 'not-a-photo-key' WHERE employee_code = 'RS-2'")
        self.photo_keys.pop()
        employees.archive_bulk(self.admin, [tokens[2][0].employee_id], "Test removal")
        meals = MealService(self.db, lock_hours=12)
        shared = ScanAppService(self.db, meals, ScanReceiptService(self.db))
        browser = secrets.token_bytes(32)
        shared.read(browser, uuid4(), tokens[0][1], self.types["LUNCH"])
        shared.read(browser, uuid4(), tokens[1][1], self.types["LUNCH"])
        shared.read(browser, uuid4(), "unknown-" + uuid4().hex, self.types["LUNCH"])
        visitor = qr.issue_master(self.admin, company_name="Visitors", contact_name="Lead", email="v@example.test",
                                  phone="+91 98765 43210", meal_limit=2, meal_type_ids=[self.types["LUNCH"], self.types["DINNER"]],
                                  department_id=department)
        shared.read(browser, uuid4(), visitor.token, self.types["DINNER"])
        meal_id = self.query("SELECT MIN(id) AS id FROM meals")[0]["id"]
        meals.correct_meal_type(self.admin, meal_id, self.types["DINNER"], "Wrong counter")
        self.execute(f"INSERT INTO `{self.name}`.meal_voids (meal_id, voided_by_staff_id, reason) "
                     f"VALUES (%s, %s, 'Old removal')", (meal_id, self.admin_id))
        phones = ScannerPhoneService(self.db, secrets.token_bytes(32))
        self.phones = [phones.register_secret_activation(secrets.token_bytes(32))["id"] for _ in range(3)]
        self.code = phones.create_code(self.admin, "Unused code", date.today() + timedelta(days=30))["id"]
        selfie_key = "shift-selfies/" + secrets.token_hex(32) + ".jpg"
        self.execute(f"UPDATE `{self.name}`.waiter_shift_settings SET latitude = 18.52, longitude = 73.85, radius_m = 100, "
                     f"updated_by_staff_id = %s WHERE id = 1", (self.admin_id,))
        self.execute(
            f"INSERT INTO `{self.name}`.waiter_shifts (phone_id, waiter_name, selfie_object_key, latitude, longitude, "
            f"accuracy_m, consent_notice_version, consent_at, started_at, expires_at) VALUES "
            f"(%s, 'Earlier shift', %s, 18.52, 73.85, 10, '2026-10-26', UTC_TIMESTAMP(6), UTC_TIMESTAMP(6), "
            f"UTC_TIMESTAMP(6) + INTERVAL 3 HOUR)",
            (self.phones[0], selfie_key),
        )
        self.photo_keys.append(selfie_key)

    def reset(self, **changes):
        from meal_management.test_data_reset import TestDataReset

        values = {"settings": self.settings, "expect_host": self.settings.db_host, "expect_db": self.name,
                  "operator_email": "reset-admin@example.test", "output": self.lines.append}
        values.update(changes)
        return TestDataReset(**values)

    def confirmation(self):
        from meal_management.test_data_reset import confirmation_text

        return confirmation_text(self.name, self.settings.db_host)

    def every_table(self):
        tables = [row["name"] for row in self.query(
            "SELECT TABLE_NAME AS name FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s ORDER BY TABLE_NAME", (self.name,))]
        checksums = {}
        for table in tables:
            checksums[table] = self.query(f"CHECKSUM TABLE `{table}` EXTENDED")[0]["Checksum"]
        return checksums

    def guards(self):
        return (
            self.query("SELECT TRIGGER_NAME, EVENT_OBJECT_TABLE, ACTION_TIMING, EVENT_MANIPULATION, ACTION_STATEMENT, "
                       "CREATED, DEFINER, SQL_MODE FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = %s "
                       "ORDER BY TRIGGER_NAME", (self.name,)),
            self.query("SELECT CONSTRAINT_NAME, CHECK_CLAUSE FROM information_schema.CHECK_CONSTRAINTS "
                       "WHERE CONSTRAINT_SCHEMA = %s ORDER BY CONSTRAINT_NAME", (self.name,)),
            self.query("SELECT TABLE_NAME, CONSTRAINT_NAME, CONSTRAINT_TYPE, ENFORCED FROM information_schema.TABLE_CONSTRAINTS "
                       "WHERE CONSTRAINT_SCHEMA = %s ORDER BY TABLE_NAME, CONSTRAINT_NAME", (self.name,)),
        )

    def counters(self):
        return {row["name"]: row["next_id"] for row in self.query(
            "SELECT TABLE_NAME AS name, AUTO_INCREMENT AS next_id FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = %s AND AUTO_INCREMENT IS NOT NULL", (self.name,))}

    def count(self, table):
        return self.query(f"SELECT COUNT(*) AS count FROM `{table}`")[0]["count"]

    def test_dry_run_reports_counts_phones_s3_and_run_order_and_changes_nothing(self):
        before, guards = self.every_table(), self.guards()
        s3 = FakeS3(versioning=None, listing=None)
        result = self.reset(s3_client=s3, s3_bucket="photos-bucket").dry_run()
        self.assertEqual(self.every_table(), before)
        self.assertEqual(self.guards(), guards)
        self.assertEqual(s3.deleted, [])
        text = "\n".join(self.lines)
        self.assertIn("  employees: 3 rows", text)
        self.assertIn("  meals: 3 rows", text)
        self.assertIn("  master_qr_meal_types: 2 rows", text)
        self.assertIn("-> revoke", text)
        self.assertIn("S3 versioning: not readable with this role. Check Properties > Bucket Versioning", text)
        self.assertIn("S3 listing: not permitted", text)
        self.assertIn("2 employee photos and 1 shift selfies are recorded in the database", text)
        self.assertIn("Photo keys with an unexpected format, never deleted: 1", text)
        self.assertIn("1. docker compose --env-file .env.docker stop admin scanner", text)
        self.assertIn("Dry run only: nothing was changed.", text)
        self.assertEqual(sorted(result["keys"]), sorted(self.photo_keys))
        self.lines.clear()
        self.reset(s3_client=FakeS3(versioning="Enabled", listing=["a.jpg", "shift-selfies/b.jpg"]), s3_bucket="b").dry_run()
        self.assertIn("S3 versioning: Enabled. Deleting adds delete markers", "\n".join(self.lines))
        self.assertIn("S3 listing: 1 objects outside shift-selfies/ and 1 under shift-selfies/.", "\n".join(self.lines))

    def test_refusals_leave_the_database_identical(self):
        from meal_management.errors import DomainError

        before = self.every_table()
        directory = self.output_dir.name
        cases = (
            (lambda: self.reset().run(True, None, self.confirmation(), directory), "RESET_SNAPSHOT_ID_REQUIRED"),
            (lambda: self.reset().run(True, "bad snapshot!", self.confirmation(), directory), "RESET_SNAPSHOT_ID_REQUIRED"),
            (lambda: self.reset().run(False, "snap-1", self.confirmation(), directory), "RESET_CONFIRM_REQUIRED"),
            (lambda: self.reset().run(True, "snap-1", "WIPE something else", directory), "RESET_CONFIRMATION_MISMATCH"),
            (lambda: self.reset(expect_host="other-host.example.test").dry_run(), "RESET_HOST_MISMATCH"),
            (lambda: self.reset(expect_db="meal_management").dry_run(), "RESET_DATABASE_MISMATCH"),
            (lambda: self.reset(keep_phones=(999999,)).dry_run(), "RESET_KEEP_PHONE_NOT_ACTIVE"),
            (lambda: self.reset(keep_phones=(self.code,)).dry_run(), "RESET_KEEP_PHONE_NOT_ACTIVE"),
            (lambda: self.reset(operator_email="reset-waiter@example.test").dry_run(), "RESET_OPERATOR_NOT_ADMIN"),
            (lambda: self.reset(operator_email="nobody@example.test").dry_run(), "RESET_OPERATOR_NOT_FOUND"),
            (lambda: self.reset(settings=self.restricted_settings()).dry_run(), "RESET_DB_ACCOUNT_LACKS_DROP"),
            (lambda: self.reset().run(True, "snap-1", self.confirmation(), directory, include_s3=True), "RESET_S3_BUCKET_REQUIRED"),
        )
        for call, code in cases:
            with self.subTest(code=code), self.assertRaisesRegex(DomainError, code):
                call()
        self.assertEqual(self.every_table(), before)

    def production_staff_shape(self):
        self.execute(f"UPDATE `{self.name}`.staff_accounts SET display_name = 'admin', email = 'mcciaexplore@gmail.com' WHERE id = 2")
        rows = self.query("SELECT s.id, s.display_name, s.email, s.is_active, s.is_scanner, r.role_code FROM staff_accounts s "
                          "JOIN staff_account_roles r ON r.staff_id = s.id WHERE s.id IN (1, 2) ORDER BY s.id")
        self.assertEqual([(row["id"], row["is_active"], row["is_scanner"], row["role_code"]) for row in rows],
                         [(1, 1, 1, "WAITER"), (2, 1, 0, "ADMIN")])
        self.assertEqual((rows[1]["display_name"], rows[1]["email"]), ("admin", "mcciaexplore@gmail.com"))
        self.assertEqual({row["code"] for row in self.query("SELECT code FROM roles")}, {"ADMIN", "AUDITOR", "WAITER"})

    def role_settings(self, default_role=True):
        grants = " ".join(row[0] for row in self.rows("SHOW GRANTS"))
        if "ROLE_ADMIN" not in grants and "ALL PRIVILEGES ON *.*" not in grants:
            self.skipTest("the MySQL test account needs ROLE_ADMIN to grant a role (GRANT ROLE_ADMIN ON *.* TO <test account>)")
        suffix = uuid.uuid4().hex[:10]
        role, user = "rrole_" + suffix, "rmaster_" + suffix
        password = secrets.token_urlsafe(24) + "aA1!"
        self.execute("CREATE ROLE %s@%s", (role, "%"))
        self.addCleanup(self.execute, "DROP ROLE IF EXISTS %s@%s", (role, "%"))
        self.assertTrue(self.name.startswith("meal_management_test_"))
        self.execute("GRANT SELECT, INSERT, UPDATE, DELETE, DROP, ALTER ON `meal\\_management\\_test\\_%`.* TO %s@%s",
                     (role, "%"))
        self.execute("CREATE USER %s@%s IDENTIFIED BY %s", (user, "%", password))
        self.addCleanup(self.execute, "DROP USER IF EXISTS %s@%s", (user, "%"))
        self.execute("GRANT %s@%s TO %s@%s", (role, "%", user, "%"))
        if default_role:
            self.execute("SET DEFAULT ROLE ALL TO %s@%s", (user, "%"))
        return replace(self.settings, db_user=user, db_password=password), user

    def test_role_based_master_account_and_production_staff_rows_can_reset(self):
        from meal_management.database import Database
        from meal_management.errors import DomainError

        self.production_staff_shape()
        settings, user = self.role_settings()
        grantee = "'" + user + "'@'%'"
        with Database(settings).transaction() as tx:
            old = tx.one(
                "SELECT (EXISTS (SELECT 1 FROM information_schema.USER_PRIVILEGES WHERE GRANTEE = %s AND PRIVILEGE_TYPE = 'DROP') "
                "OR EXISTS (SELECT 1 FROM information_schema.SCHEMA_PRIVILEGES WHERE GRANTEE = %s AND PRIVILEGE_TYPE = 'DROP' "
                "AND %s LIKE TABLE_SCHEMA)) AS allowed", (grantee, grantee, self.name))
            self.assertEqual(old["allowed"], 0)
        self.reset(settings=settings, operator_email="mcciaexplore@gmail.com").dry_run()
        self.assertIn("Dry run only: nothing was changed.", "\n".join(self.lines))
        result = self.reset(settings=settings, operator_email="mcciaexplore@gmail.com").run(
            True, "snap-role", self.confirmation(), self.output_dir.name)
        self.assertEqual(result["event_id"], 1)
        self.assertEqual(self.query("SELECT actor_staff_id FROM audit_events"), [{"actor_staff_id": 2}])
        self.assertEqual(self.count("employees"), 0)

        inactive, _ = self.role_settings(default_role=False)
        with self.assertRaisesRegex(DomainError, "RESET_DB_ROLE_NOT_ACTIVE: .*granted but inactive roles: `rrole_"):
            self.reset(settings=inactive, operator_email="mcciaexplore@gmail.com").dry_run()

    def test_production_staff_rows_pass_and_operator_refusals_name_the_condition(self):
        from meal_management.test_data_reset import ResetRefused

        self.production_staff_shape()
        self.reset(operator_email="mcciaexplore@gmail.com").dry_run()
        self.assertIn("Dry run only: nothing was changed.", "\n".join(self.lines))
        self.execute(f"UPDATE `{self.name}`.staff_accounts SET is_active = 0 WHERE id = 2")
        with self.assertRaises(ResetRefused) as caught:
            self.reset(operator_email="mcciaexplore@gmail.com").dry_run()
        self.assertEqual((caught.exception.code, caught.exception.detail),
                         ("RESET_OPERATOR_INACTIVE", "staff account 2 (mcciaexplore@gmail.com) is inactive"))
        with self.assertRaises(ResetRefused) as caught:
            self.reset(operator_email="meal-scanner@system.invalid").dry_run()
        self.assertEqual((caught.exception.code, caught.exception.detail),
                         ("RESET_OPERATOR_IS_SCANNER", "staff account 1 (meal-scanner@system.invalid) is a scanner account"))

    def restricted_settings(self):
        from meal_management import runtime_account

        user = "mrt_" + uuid.uuid4().hex[:12]
        password = secrets.token_urlsafe(24) + "aA1!"
        self.execute("CREATE USER %s@%s IDENTIFIED BY %s", (user, "%", password))
        self.addCleanup(self.execute, "DROP USER IF EXISTS %s@%s", (user, "%"))
        for row in self.query("SELECT TABLE_NAME AS name FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s", (self.name,)):
            self.execute(f"GRANT SELECT ON `{self.name}`.`{row['name']}` TO %s@%s", (user, "%"))
        for privilege, tables in (("INSERT", runtime_account.INSERT_TABLES), ("UPDATE", runtime_account.UPDATE_TABLES),
                                  ("DELETE", runtime_account.DELETE_TABLES)):
            for table in tables:
                self.execute(f"GRANT {privilege} ON `{self.name}`.`{table}` TO %s@%s", (user, "%"))
        return replace(self.settings, db_user=user, db_password=password)

    def test_real_run_wipes_test_data_and_keeps_staff_settings_guards_and_counters(self):
        from meal_management.test_data_reset import KEEP_TABLES, WIPE_TABLES

        kept_before = {name: value for name, value in self.every_table().items() if name in KEEP_TABLES}
        guards, counters = self.guards(), self.counters()
        s3 = FakeS3(versioning="Suspended")
        result = self.reset(keep_phones=(self.phones[1],), s3_client=s3, s3_bucket="photos-bucket").run(
            True, "rds:mccia-meal-db-2026-10-26-07-00", self.confirmation(), self.output_dir.name, include_s3=True)
        for table in WIPE_TABLES:
            with self.subTest(table=table):
                self.assertEqual(self.count(table), 1 if table == "audit_events" else 0)
        self.assertEqual({name: value for name, value in self.every_table().items() if name in KEEP_TABLES}, kept_before)
        self.assertEqual(self.guards(), guards)
        audit = self.query("SELECT id, action, entity_id, after_data FROM audit_events")
        self.assertEqual([(row["id"], row["action"], row["entity_id"]) for row in audit], [(1, "TEST_DATA_RESET", self.name)])
        self.assertIn("rds:mccia-meal-db-2026-10-26-07-00", audit[0]["after_data"])
        after_counters = self.counters()
        for table in ("employees", "meals", "servings", "qr_credentials", "scan_attempts", "waiter_shifts", "email_queue"):
            with self.subTest(counter=table):
                self.assertEqual(after_counters[table], counters[table])
        phones = {row["id"]: row["revoked_at"] is not None for row in self.query("SELECT id, revoked_at FROM scanner_phones")}
        self.assertEqual(phones, {self.phones[0]: True, self.phones[1]: False, self.phones[2]: True, self.code: True})
        self.assertEqual(sorted(key for _, key in s3.deleted), sorted(self.photo_keys))
        self.assertNotIn("not-a-photo-key", [key for _, key in s3.deleted])
        self.assertEqual(result["event_id"], 1)
        self.assertEqual(self.query("SELECT COUNT(*) AS count FROM waiter_shift_settings WHERE latitude IS NOT NULL")[0]["count"], 1)
        text = "\n".join(self.lines)
        self.assertIn("RDS snapshot: rds:mccia-meal-db-2026-10-26-07-00", text)
        self.assertIn("Audit log restarted with TEST_DATA_RESET as entry 1.", text)
        self.assertIn("Triggers, CHECK constraints, foreign keys and the schema fingerprint are identical to before.", text)
        self.assertIn("S3 versioning: Suspended", text)
        self.assertIn("Next: docker compose --env-file .env.docker up -d", text)
        self.assert_application_works_and_guards_refuse()

    def assert_application_works_and_guards_refuse(self):
        from meal_management.accounts import StaffService
        from meal_management.employees import EmployeeService
        from meal_management.meals import MealService
        from meal_management.qr import QrService
        from meal_management.scan_app import ScanAppService
        from meal_management.scan_receipts import ScanReceiptService
        from meal_management.security import QrRenderer, TokenVault

        staff = StaffService(self.db)
        context = staff.authenticate("reset-admin@example.test", self.password)
        qr = QrService(self.db, TokenVault(self.settings.qr_encryption_keys), QrRenderer())
        employees = EmployeeService(self.db, qr)
        department = self.query("SELECT id FROM departments LIMIT 1")[0]["id"]
        registration = employees.register(context, "NEW-1", "New Employee", "new1@example.test", department,
                                          company_name="Example", phone="+91 98765 40000")
        token = qr.email_queue.preview(context, registration.email_id).token
        shared = ScanAppService(self.db, MealService(self.db), ScanReceiptService(self.db))
        self.assertTrue(shared.read(secrets.token_bytes(32), uuid4(), token, self.types["LUNCH"]).approved)
        visitor = qr.issue_master(context, company_name="V", contact_name="L", email="l@example.test",
                                  phone="+91 98765 43210", meal_limit=1, meal_type_ids=[self.types["LUNCH"]],
                                  department_id=department)
        self.assertTrue(shared.read(secrets.token_bytes(32), uuid4(), visitor.token, self.types["LUNCH"]).approved)
        for table in ("qr_credentials", "meals", "servings", "master_qr_meal_types", "audit_events", "scan_attempts"):
            with self.subTest(guard=table):
                self.assertEqual(self.errno(f"DELETE FROM `{table}`"), 1644)

    def test_interrupted_run_is_completed_safely_by_a_rerun(self):
        from meal_management.errors import DomainError
        from meal_management.test_data_reset import WIPE_TABLES

        guards, counters = self.guards(), self.counters()

        def interrupt(table):
            if table == "audit_events":
                raise DomainError("SIMULATED_INTERRUPTION")

        with self.assertRaisesRegex(DomainError, "SIMULATED_INTERRUPTION"):
            self.reset(truncate_hook=interrupt).run(True, "snap-1", self.confirmation(), self.output_dir.name)
        self.assertEqual(self.count("meals"), 0)
        self.assertEqual(self.count("employees"), 0)
        self.assertGreater(self.count("audit_events"), 0)
        self.assertEqual(self.query("SELECT @@GLOBAL.foreign_key_checks AS checks")[0]["checks"], 1)
        s3 = FakeS3()
        self.reset(s3_client=s3, s3_bucket="photos-bucket").run(True, "snap-1", self.confirmation(), self.output_dir.name, include_s3=True)
        for table in WIPE_TABLES:
            with self.subTest(table=table):
                self.assertEqual(self.count(table), 1 if table == "audit_events" else 0)
        self.assertEqual(self.guards(), guards)
        after = self.counters()
        for table in ("employees", "meals", "qr_credentials", "waiter_shifts"):
            with self.subTest(counter=table):
                self.assertEqual(after[table], counters[table])
        self.assertEqual(sorted(key for _, key in s3.deleted), sorted(self.photo_keys))
        self.assertEqual(self.query("SELECT id, action FROM audit_events"), [{"id": 1, "action": "TEST_DATA_RESET"}])

    def test_a_second_full_run_is_harmless(self):
        self.reset().run(True, "snap-1", self.confirmation(), self.output_dir.name)
        guards = self.guards()
        self.reset().run(True, "snap-2", self.confirmation(), self.output_dir.name)
        self.assertEqual(self.guards(), guards)
        self.assertEqual([row["action"] for row in self.query("SELECT action FROM audit_events")], ["TEST_DATA_RESET"])
        self.assertEqual(self.count("employees"), 0)
