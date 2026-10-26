import secrets
import time
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from test_mysql_services import MealServicesFixture

MIGRATIONS = Path(__file__).resolve().parents[2] / "database" / "migrations"


class VisitorMealTypeMySQLTests(MealServicesFixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from meal_management.meals import MealService
        from meal_management.scan_app import ScanAppService
        from meal_management.scan_receipts import ScanReceiptService

        cls.type_ids = {row["code"]: row["id"] for row in cls.rows_static("SELECT id, code FROM meal_types")}
        cls.shared = ScanAppService(cls.db, MealService(cls.db, lock_hours=12), ScanReceiptService(cls.db))

    @classmethod
    def rows_static(cls, statement, parameters=()):
        cursor = cls.admin_connection.cursor(dictionary=True)
        try:
            cursor.execute(statement, parameters)
            return cursor.fetchall()
        finally:
            cursor.close()

    def visitor_qr(self, codes, people=2, expires_at=None):
        return self.qr.issue_master(
            self.admin_context, company_name="Example Visitors", contact_name="Visitor Lead",
            email="visitors@example.test", phone="+91 98765 43210", meal_limit=people,
            meal_type_ids=[self.type_ids[code] for code in codes], expires_at=expires_at,
        )

    def read(self, credential, code, request_id=None, browser=None):
        return self.shared.read(browser or self.browser, request_id or uuid4(), credential.token, self.type_ids[code])

    def allowances(self, qr_id):
        rows = self.rows(
            "SELECT t.code, a.meal_limit, a.meals_used, a.exhausted_at IS NOT NULL AS exhausted "
            "FROM master_qr_meal_types a JOIN meal_types t ON t.id = a.meal_type_id WHERE a.qr_id = %s",
            (qr_id,),
        )
        return {row["code"]: (row["meals_used"], row["meal_limit"], bool(row["exhausted"])) for row in rows}

    def summary(self, qr_id):
        row = self.rows("SELECT meal_limit, meals_used, exhausted_at FROM master_qr_allocations WHERE qr_id = %s", (qr_id,))[0]
        return row["meals_used"], row["exhausted_at"] is not None

    def setUp(self):
        super().setUp()
        self.browser = secrets.token_bytes(32)

    def admin_errno(self, sql, params=()):
        cursor = self.admin_connection.cursor()
        try:
            cursor.execute(sql, params)
        except Exception as error:
            return getattr(error, "errno", None)
        finally:
            cursor.close()
        return None

    def test_each_allowed_type_has_its_own_allowance_and_the_final_meal_expires_the_qr(self):
        credential = self.visitor_qr(["LUNCH", "DINNER"], people=2)
        self.assertEqual(self.allowances(credential.qr_id), {"LUNCH": (0, 2, False), "DINNER": (0, 2, False)})
        first, second = self.read(credential, "LUNCH"), self.read(credential, "LUNCH")
        self.assertTrue(first.approved and second.approved, "visitors have no 12-hour lock")
        self.assertEqual(self.allowances(credential.qr_id)["LUNCH"], (2, 2, True))
        self.assertEqual(self.summary(credential.qr_id), (0, False))
        used_up = self.read(credential, "LUNCH")
        self.assertEqual((used_up.approved, used_up.code), (False, "MASTER_MEAL_TYPE_USED_UP"))
        self.assertTrue(self.read(credential, "DINNER").approved)
        self.assertEqual(self.summary(credential.qr_id), (1, False))
        self.assertTrue(self.read(credential, "DINNER").approved)
        self.assertEqual(self.allowances(credential.qr_id), {"LUNCH": (2, 2, True), "DINNER": (2, 2, True)})
        self.assertEqual(self.summary(credential.qr_id), (2, True))
        for code in ("LUNCH", "DINNER", "BREAKFAST"):
            with self.subTest(code=code):
                self.assertEqual(self.read(credential, code).code, "QR_EXPIRED")
        meals = self.rows(
            "SELECT t.code, COUNT(*) AS meals FROM servings s JOIN meals m ON m.serving_id = s.id "
            "JOIN meal_types t ON t.id = s.meal_type_id WHERE s.qr_id = %s GROUP BY t.code", (credential.qr_id,),
        )
        self.assertEqual({row["code"]: row["meals"] for row in meals}, {"LUNCH": 2, "DINNER": 2})

    def test_disallowed_type_is_refused_without_using_an_allowance_or_recording_a_meal(self):
        credential = self.visitor_qr(["LUNCH"], people=3)
        self.assertTrue(self.read(credential, "LUNCH").approved)
        before = (self.allowances(credential.qr_id), self.summary(credential.qr_id),
                  self.scalar("SELECT COUNT(*) FROM servings WHERE qr_id = %s", (credential.qr_id,)),
                  self.scalar("SELECT COUNT(*) FROM meals"))
        refused = self.read(credential, "DINNER")
        self.assertEqual((refused.approved, refused.code, refused.serving_id), (False, "MASTER_MEAL_TYPE_NOT_ALLOWED", None))
        after = (self.allowances(credential.qr_id), self.summary(credential.qr_id),
                 self.scalar("SELECT COUNT(*) FROM servings WHERE qr_id = %s", (credential.qr_id,)),
                 self.scalar("SELECT COUNT(*) FROM meals"))
        self.assertEqual(after, before)
        self.assertEqual(self.scalar(
            "SELECT COUNT(*) FROM scan_attempts WHERE qr_id = %s AND rejection_code = 'MASTER_MEAL_TYPE_NOT_ALLOWED'",
            (credential.qr_id,)), 1)

    def test_retry_of_the_same_scan_returns_the_original_meal_and_uses_one_allowance(self):
        credential = self.visitor_qr(["LUNCH"], people=3)
        request_id = uuid4()
        first = self.read(credential, "LUNCH", request_id)
        retry = self.read(credential, "LUNCH", request_id)
        self.assertTrue(first.approved and retry.approved)
        self.assertEqual((retry.serving_id, retry.meal_ids), (first.serving_id, first.meal_ids))
        self.assertTrue(retry.duplicate)
        self.assertEqual(self.allowances(credential.qr_id)["LUNCH"], (1, 3, False))
        self.assertEqual(self.summary(credential.qr_id), (1, False))

    def test_simultaneous_scans_for_the_last_meal_of_a_type_approve_once(self):
        credential = self.visitor_qr(["LUNCH", "DINNER"], people=1)
        barrier = threading.Barrier(4)

        def scan(_):
            barrier.wait(timeout=10)
            return self.read(credential, "LUNCH", browser=secrets.token_bytes(32))

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(scan, range(4)))
        self.assertEqual(sum(result.approved for result in results), 1)
        self.assertEqual({result.code for result in results if not result.approved}, {"MASTER_MEAL_TYPE_USED_UP"})
        self.assertEqual(self.allowances(credential.qr_id)["LUNCH"], (1, 1, True))
        self.assertEqual(self.summary(credential.qr_id), (0, False))

    def test_earlier_expiry_refuses_every_type(self):
        credential = self.visitor_qr(["LUNCH", "DINNER"], people=2,
                                     expires_at=datetime.now(timezone.utc) + timedelta(seconds=2))
        time.sleep(2.5)
        for code in ("LUNCH", "DINNER"):
            with self.subTest(code=code):
                self.assertEqual(self.read(credential, code).code, "QR_EXPIRED")
        self.assertEqual(self.allowances(credential.qr_id), {"LUNCH": (0, 2, False), "DINNER": (0, 2, False)})

    def test_creation_needs_active_types_and_writes_nothing_otherwise(self):
        from meal_management.errors import DomainError

        before = self.scalar("SELECT COUNT(*) FROM qr_credentials")
        cursor = self.admin_connection.cursor()
        try:
            cursor.execute("INSERT INTO meal_types (code, name, is_active) VALUES (%s, 'Retired', FALSE)",
                           ("RETIRED-" + uuid4().hex[:8],))
            retired = cursor.lastrowid
        finally:
            cursor.close()
        for ids, code in (([self.type_ids["LUNCH"], retired], "MEAL_TYPE_UNAVAILABLE"), ([], "INVALID_MEAL_TYPE_IDS"),
                          (None, "INVALID_MEAL_TYPE_IDS")):
            with self.subTest(ids=ids), self.assertRaisesRegex(DomainError, code):
                self.qr.issue_master(self.admin_context, company_name="Example", contact_name="Lead",
                                     email="lead@example.test", phone="+91 98765 43210", meal_limit=2, meal_type_ids=ids)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM qr_credentials"), before)

    def test_listing_and_detail_show_remaining_per_type(self):
        from meal_management.queries import QueryService

        credential = self.visitor_qr(["LUNCH", "DINNER"], people=2)
        self.assertTrue(self.read(credential, "DINNER").approved)
        queries = QueryService(self.db)
        detail = queries.qr_metadata(self.admin_context, credential.qr_id)
        summary = {row["code"]: (row["meals_remaining"], row["meal_limit"]) for row in detail["meal_types"]}
        self.assertEqual(summary, {"LUNCH": (2, 2), "DINNER": (1, 2)})
        self.assertEqual(detail["meals_remaining"], 2)
        listed = {item["id"]: item for item in queries.master_qrs(self.admin_context, limit=200)["items"]}
        self.assertEqual(listed[credential.qr_id]["meal_types"], detail["meal_types"])

    def test_database_keeps_allowances_append_only(self):
        credential = self.visitor_qr(["LUNCH"], people=2)
        self.assertTrue(self.read(credential, "LUNCH").approved)
        for sql in ("DELETE FROM master_qr_meal_types WHERE qr_id = %s",
                    "UPDATE master_qr_meal_types SET meal_limit = 9 WHERE qr_id = %s",
                    "UPDATE master_qr_meal_types SET meals_used = 0 WHERE qr_id = %s",
                    "UPDATE master_qr_meal_types SET meal_type_id = meal_type_id + 0 + 1 WHERE qr_id = %s"):
            with self.subTest(sql=sql):
                self.assertIn(self.admin_errno(sql, (credential.qr_id,)), {1644, 1452})
        self.assertEqual(self.allowances(credential.qr_id)["LUNCH"], (1, 2, False))


class VisitorMealTypeBackfillMySQLTests(MealServicesFixture):
    def test_migration_014_gives_each_existing_visitor_qr_its_remaining_count_per_active_type(self):
        from meal_management.accounts import StaffService
        from meal_management.database import Database
        from meal_management.migrations import MigrationRunner
        from meal_management.qr import QrService
        from meal_management.security import QrRenderer

        name = f"{self.database_name[:40]}_{uuid.uuid4().hex[:6]}_test"
        cursor = self.admin_connection.cursor()
        cursor.execute(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci")
        cursor.close()
        self.addCleanup(self._drop, name)
        database = Database(replace(self.admin_settings, db_name=name))
        self.assertEqual(MigrationRunner(database, MIGRATIONS, through=13).apply()[-1], 13)
        staff = StaffService(database)
        password = secrets.token_urlsafe(24)
        admin = staff.bootstrap_admin("Backfill Admin", "backfill@example.test", password)
        context = staff.authenticate("backfill@example.test", password)
        qr = QrService(database, self.vault, QrRenderer())
        legacy = qr.issue_master(context).qr_id
        groups = {}
        with database.transaction() as tx:
            for label, limit, used in (("fresh", 5, 0), ("partly", 5, 2), ("used-up", 3, 3)):
                qr_id = qr._issue(tx, admin, "MASTER", None, None).qr_id
                tx.execute(
                    "INSERT INTO master_qr_allocations (qr_id, company_name, contact_name, email, phone, meal_limit, "
                    "meals_used, created_by_staff_id, exhausted_at) VALUES (%s, 'Example', 'Lead', 'lead@example.test', "
                    "'+919876543210', %s, %s, %s, CASE WHEN %s = %s THEN UTC_TIMESTAMP(6) ELSE NULL END)",
                    (qr_id, limit, used, admin, limit, used),
                )
                groups[label] = qr_id
            tx.execute("INSERT INTO meal_types (code, name, is_active) VALUES ('OLD-SNACK', 'Old snack', FALSE)")
            active = {row["code"] for row in tx.all("SELECT code FROM meal_types WHERE is_active = 1")}
        self.assertEqual(MigrationRunner(database, MIGRATIONS).apply(), (14,))
        with database.transaction() as tx:
            rows = tx.all(
                "SELECT a.qr_id, t.code, a.meal_limit, a.meals_used, a.exhausted_at, x.exhausted_at AS allocation_exhausted "
                "FROM master_qr_meal_types a JOIN meal_types t ON t.id = a.meal_type_id "
                "JOIN master_qr_allocations x ON x.qr_id = a.qr_id ORDER BY a.qr_id, t.code"
            )
        self.assertEqual({row["code"] for row in rows}, active)
        self.assertNotIn("OLD-SNACK", {row["code"] for row in rows})
        self.assertNotIn(legacy, {row["qr_id"] for row in rows})
        for label, limit, used in (("fresh", 5, 0), ("partly", 5, 2), ("used-up", 3, 3)):
            mine = [row for row in rows if row["qr_id"] == groups[label]]
            with self.subTest(label=label):
                self.assertEqual(len(mine), len(active))
                self.assertEqual({(row["meal_limit"], row["meals_used"]) for row in mine}, {(limit, used)})
                self.assertTrue(all(row["exhausted_at"] == row["allocation_exhausted"] for row in mine))

    def _drop(self, name):
        cursor = self.admin_connection.cursor()
        try:
            cursor.execute(f"DROP DATABASE `{name}`")
        finally:
            cursor.close()
