import secrets
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from uuid import uuid4

from mysql_support import IsolatedMySQLTestCase, MYSQL_TESTS_ENABLED


class DevelopmentSeedFixture:
    def initialize_seed_services(self):
        from meal_management.accounts import StaffService
        from meal_management.development_seed import DevelopmentSeedService
        from meal_management.qr import QrService
        from meal_management.runtime import RuntimeSettings
        from meal_management.security import QrRenderer, TokenVault

        self.staff = StaffService(self.db)
        self.admin_password = secrets.token_urlsafe(32)
        self.admin_id = self.staff.bootstrap_admin("Seed integration administrator", "seed.admin@example.test", self.admin_password)
        self.admin = self.staff.authenticate("seed.admin@example.test", self.admin_password)
        self.waiter_passwords = (secrets.token_urlsafe(32), secrets.token_urlsafe(32))
        self.runtime = RuntimeSettings(csrf_secret=secrets.token_bytes(32), login_rate_secret=secrets.token_bytes(32))
        self.vault = TokenVault(self.settings.qr_encryption_keys)
        self.qr = QrService(self.db, self.vault, QrRenderer())
        self.seed = DevelopmentSeedService(self.db, self.runtime, self.vault, QrRenderer())


@unittest.skipUnless(MYSQL_TESTS_ENABLED, "MySQL connection and disposable-schema changes are not enabled")
class MySQLDevelopmentSeedLifecycleTests(DevelopmentSeedFixture, IsolatedMySQLTestCase):
    def test_real_seed_rerun_gallery_and_scan_scenarios(self):
        from meal_management.development_seed import EXPECTED_KEYS, MASTER_LABEL, SEED_VERSION, WAITERS
        from meal_management.errors import DomainError
        from meal_management.meals import MealService
        from meal_management.models import ScanInput

        self.initialize_seed_services()
        unrelated_master = self.qr.issue_master(self.admin)
        unrelated_before = self.rows("SELECT * FROM qr_credentials WHERE id = %s", (unrelated_master.qr_id,))[0]
        created = self.seed.seed(self.admin, self.waiter_passwords)
        self.assertTrue(created.created)
        time.sleep(created.expiry_wait_seconds)
        manifest = self.seed.manifest(self.admin)
        self.assertEqual(len(manifest["employees"]), 10)
        self.assertEqual(len(manifest["waiters"]), 2)
        self.assertEqual(manifest["master"]["label"], MASTER_LABEL)
        self.assertNotEqual(manifest["master"]["qr_id"], unrelated_master.qr_id)
        self.assertEqual(
            self.rows("SELECT * FROM qr_credentials WHERE id = %s", (unrelated_master.qr_id,))[0],
            unrelated_before,
        )
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM development_seed_records WHERE seed_version = %s", (SEED_VERSION,)), len(EXPECTED_KEYS))
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM employees"), 10)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM email_queue"), 10)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM staff_accounts WHERE is_scanner = 0"), 3)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM qr_credentials"), 12)
        registry_before = self.rows("SELECT * FROM development_seed_records ORDER BY fixture_key")
        qr_before = self.rows("SELECT * FROM qr_credentials ORDER BY id")
        emails_before = self.rows("SELECT * FROM email_queue ORDER BY id")
        allocations_before = self.rows("SELECT * FROM master_qr_allocations ORDER BY qr_id")
        staff_before = self.rows("SELECT id, email, password_hash FROM staff_accounts ORDER BY id")
        repeated = self.seed.seed(self.admin)
        self.assertFalse(repeated.created)
        self.assertEqual(self.rows("SELECT * FROM development_seed_records ORDER BY fixture_key"), registry_before)
        self.assertEqual(self.rows("SELECT * FROM qr_credentials ORDER BY id"), qr_before)
        self.assertEqual(self.rows("SELECT * FROM email_queue ORDER BY id"), emails_before)
        self.assertEqual(self.rows("SELECT * FROM master_qr_allocations ORDER BY qr_id"), allocations_before)
        self.assertEqual(self.rows("SELECT id, email, password_hash FROM staff_accounts ORDER BY id"), staff_before)
        self.assertEqual(
            self.rows(
                "SELECT r.role_code FROM staff_account_roles r JOIN staff_accounts s ON s.id = r.staff_id "
                "WHERE s.email IN (%s, %s) ORDER BY s.email, r.role_code",
                (WAITERS[0][2], WAITERS[1][2]),
            ),
            [{"role_code": "WAITER"}, {"role_code": "WAITER"}],
        )
        waiter = self.staff.authenticate(WAITERS[0][2], self.waiter_passwords[0])
        with self.assertRaisesRegex(DomainError, "ROLE_REQUIRED"):
            self.seed.manifest(waiter)
        with self.assertRaisesRegex(DomainError, "ROLE_REQUIRED"):
            self.seed.export_svg(waiter, manifest["employees"][0]["qr_id"])
        with self.assertRaisesRegex(DomainError, "DEVELOPMENT_QR_NOT_FOUND"):
            self.seed.export_svg(self.admin, unrelated_master.qr_id)
        meal_type_id = self.scalar("SELECT id FROM meal_types WHERE code = %s", ("TEST-LUNCH",))
        meals = MealService(self.db)
        tokens = {}
        for employee in manifest["employees"]:
            self.assertIn("<svg", self.seed.export_svg(self.admin, employee["qr_id"]))
            archived = self.rows(
                "SELECT token_ciphertext FROM development_seed_records WHERE qr_id = %s",
                (employee["qr_id"],),
            )[0]["token_ciphertext"]
            token = self.vault.decrypt(archived)
            tokens[employee["scenario"]] = token
            self.assertNotIn(token, repr(manifest))
            scan = ScanInput(uuid4(), token, meal_type_id, "TEST-SCANNER-A")
            result = meals.record(waiter, scan)
            self.assertEqual(result.code, employee["expected_code"])
            self.assertEqual(result.approved, employee["scenario"] == "ACTIVE")
            if employee["scenario"] != "ACTIVE":
                with self.assertRaises(DomainError):
                    self.qr.export_svg(self.admin, employee["qr_id"])
            else:
                repeated_scan = meals.record(waiter, scan)
                self.assertTrue(repeated_scan.approved)
                self.assertTrue(repeated_scan.duplicate)
                self.assertEqual(result.meal_ids, repeated_scan.meal_ids)
        rejected = self.rows("SELECT rejection_code FROM scan_attempts WHERE outcome = 'REJECTED' ORDER BY id")
        self.assertEqual({row["rejection_code"] for row in rejected}, {"EMPLOYEE_INACTIVE", "QR_REVOKED", "QR_EXPIRED", "DUPLICATE_REQUEST"})
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM meals"), 7)
        master_id = manifest["master"]["qr_id"]
        master = self.qr.retrieve(self.admin, master_id)
        request_id = uuid4()
        read = meals.read(waiter, ScanInput(request_id, master.token, meal_type_id, "TEST-SCANNER-A"))
        self.assertTrue(read.approved)
        self.assertEqual(len(read.meal_ids), 1)
        retry = meals.record(waiter, ScanInput(request_id, master.token, meal_type_id, "TEST-SCANNER-A"))
        self.assertTrue(retry.approved)
        self.assertTrue(retry.duplicate)
        self.assertEqual(retry.meal_ids, read.meal_ids)
        serving = self.rows(
            "SELECT visitor_company_name, visitor_name, visitor_email, visitor_phone FROM servings WHERE id = %s",
            (read.serving_id,),
        )[0]
        self.assertEqual(serving, {
            "visitor_company_name": "TEST Example Visitors", "visitor_name": "TEST Visitor Lead",
            "visitor_email": "visitor.lead@example.test", "visitor_phone": "+1 202 555 0199",
        })
        used = self.rows("SELECT * FROM master_qr_allocations WHERE qr_id = %s", (master_id,))
        self.assertEqual((used[0]["meal_limit"], used[0]["meals_used"]), (10, 1))
        self.assertFalse(self.seed.seed(self.admin).created)
        self.assertEqual(self.rows("SELECT * FROM master_qr_allocations WHERE qr_id = %s", (master_id,)), used)


@unittest.skipUnless(MYSQL_TESTS_ENABLED, "MySQL connection and disposable-schema changes are not enabled")
class MySQLDevelopmentSeedCollisionTests(DevelopmentSeedFixture, IsolatedMySQLTestCase):
    def test_unowned_namespace_collision_rolls_back_without_overwrite(self):
        from meal_management.development_seed import DEPARTMENT_NAME
        from meal_management.employees import EmployeeService
        from meal_management.errors import DomainError

        self.initialize_seed_services()
        department_id = EmployeeService(self.db, self.qr).create_department(self.admin, DEPARTMENT_NAME)
        before = self.rows("SELECT * FROM departments WHERE id = %s", (department_id,))[0]
        with self.assertRaisesRegex(DomainError, "DEVELOPMENT_SEED_NAMESPACE_CONFLICT"):
            self.seed.seed(self.admin, self.waiter_passwords)
        self.assertEqual(self.rows("SELECT * FROM departments WHERE id = %s", (department_id,))[0], before)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM development_seed_batches"), 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM development_seed_records"), 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM employees"), 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM email_queue"), 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM qr_credentials"), 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM staff_accounts WHERE is_scanner = 0"), 1)


@unittest.skipUnless(MYSQL_TESTS_ENABLED, "MySQL connection and disposable-schema changes are not enabled")
class MySQLDevelopmentSeedConcurrencyTests(DevelopmentSeedFixture, IsolatedMySQLTestCase):
    def test_two_admin_sessions_seed_once_without_duplicate_qrs_or_emails(self):
        self.initialize_seed_services()
        second_admin = self.staff.authenticate("seed.admin@example.test", self.admin_password)
        barrier = threading.Barrier(2)

        def seed(context):
            barrier.wait(timeout=10)
            return self.seed.seed(context, self.waiter_passwords)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(seed, context) for context in (self.admin, second_admin)]
            results = [future.result(timeout=30) for future in futures]
        self.assertEqual(sorted(result.created for result in results), [False, True])
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM development_seed_batches"), 1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM employees"), 10)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM email_queue"), 10)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM qr_credentials"), 11)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM staff_accounts WHERE is_scanner = 0"), 3)


@unittest.skipUnless(MYSQL_TESTS_ENABLED, "MySQL connection and disposable-schema changes are not enabled")
class MySQLDevelopmentSeedRollbackTests(DevelopmentSeedFixture, IsolatedMySQLTestCase):
    def test_mid_seed_failure_rolls_back_real_service_writes(self):
        from meal_management.employees import EmployeeService
        from meal_management.errors import DomainError

        self.initialize_seed_services()
        scanner_defaults = {
            table: self.rows("SELECT * FROM " + table + " ORDER BY id")
            for table in ("locations", "scanner_devices", "meal_types", "scan_app_settings")
        }
        original = EmployeeService.register

        def failing_register(service, context, **values):
            if values["employee_code"] == "TEST-EMP-005":
                raise DomainError("FICTIONAL_REGISTRATION_FAILURE")
            return original(service, context, **values)

        with patch.object(EmployeeService, "register", failing_register):
            with self.assertRaisesRegex(DomainError, "FICTIONAL_REGISTRATION_FAILURE"):
                self.seed.seed(self.admin, self.waiter_passwords)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM development_seed_batches"), 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM development_seed_records"), 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM departments"), 0)
        for table, rows in scanner_defaults.items():
            self.assertEqual(self.rows("SELECT * FROM " + table + " ORDER BY id"), rows)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM employees"), 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM email_queue"), 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM qr_credentials"), 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM staff_accounts WHERE is_scanner = 0"), 1)


if __name__ == "__main__":
    unittest.main()
