import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta, timezone
from uuid import uuid4

from test_mysql_services import MealServicesFixture


class MealTypeEligibilityMySQLTests(MealServicesFixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from meal_management.meals import MealService

        cls.locked_meals = MealService(cls.db, lock_hours=12)
        cls.type_ids = {row["code"]: row["id"] for row in cls.rows_static("SELECT id, code FROM meal_types")}

    @classmethod
    def rows_static(cls, statement, parameters=()):
        cursor = cls.admin_connection.cursor(dictionary=True)
        try:
            cursor.execute(statement, parameters)
            return cursor.fetchall()
        finally:
            cursor.close()

    def typed_scan(self, token, code, request_id=None):
        return replace(self.scan(token, request_id=request_id), meal_type_id=self.type_ids[code])

    def employee_with(self, codes=None):
        suffix = uuid4().hex[:12]
        ids = None if codes is None else [self.type_ids[code] for code in codes]
        registration = self.employees.register(
            self.admin_context, f"MT-{suffix}", "Meal Type Employee", f"mealtype-{suffix}@example.test", self.department_id,
            company_name="Example Company", phone="+1 202 555 0101", meal_type_ids=ids,
        )
        token = self.qr.email_queue.preview(self.admin_context, registration.email_id).token
        return registration, token

    def test_seeded_meal_types_and_retired_scanner_meal(self):
        rows = {row["code"]: row for row in self.rows("SELECT code, name, is_active FROM meal_types")}
        for code, name in (("BREAKFAST", "Breakfast"), ("LUNCH", "Lunch"), ("HIGH-TEA", "High Tea"), ("DINNER", "Dinner")):
            self.assertEqual((rows[code]["name"], rows[code]["is_active"]), (name, 1))
        self.assertEqual(rows["SCANNER-MEAL"]["is_active"], 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = DATABASE() AND TRIGGER_NAME = 'shared_scanner_meal_type_guard'"), 0)

    def test_new_employees_default_to_all_active_types_and_can_be_restricted(self):
        registration, _ = self.employee_with()
        granted = {row["meal_type_id"] for row in self.rows("SELECT meal_type_id FROM employee_meal_types WHERE employee_id = %s", (registration.employee_id,))}
        active = {row["id"] for row in self.rows("SELECT id FROM meal_types WHERE is_active = 1")}
        self.assertEqual(granted, active)
        self.employees.set_meal_types(self.admin_context, registration.employee_id, [self.type_ids["LUNCH"]])
        self.assertEqual([row["meal_type_id"] for row in self.rows("SELECT meal_type_id FROM employee_meal_types WHERE employee_id = %s", (registration.employee_id,))], [self.type_ids["LUNCH"]])
        actions = [row["action"] for row in self.rows("SELECT action FROM audit_events WHERE entity_type = 'employees' AND entity_id = %s", (str(registration.employee_id),))]
        self.assertIn("EMPLOYEE_MEAL_TYPES_CHANGED", actions)
        from meal_management.errors import DomainError

        with self.assertRaisesRegex(DomainError, "MEAL_TYPE_UNAVAILABLE"):
            self.employees.set_meal_types(self.admin_context, registration.employee_id, [self.type_ids["SCANNER-MEAL"]])
        with self.assertRaisesRegex(DomainError, "INVALID_MEAL_TYPE_IDS"):
            self.employees.set_meal_types(self.admin_context, registration.employee_id, [])

    def test_ineligible_type_is_refused_and_retry_returns_the_same_refusal(self):
        _, token = self.employee_with(["LUNCH"])
        scan = self.typed_scan(token, "DINNER")
        refused = self.locked_meals.record(self.waiter_context, scan)
        self.assertEqual((refused.approved, refused.code), (False, "MEAL_TYPE_NOT_ELIGIBLE"))
        self.assertEqual(self.locked_meals.record(self.waiter_context, scan).code, "MEAL_TYPE_NOT_ELIGIBLE")
        self.assertTrue(self.locked_meals.record(self.waiter_context, self.typed_scan(token, "LUNCH")).approved)

    def test_same_type_is_locked_for_twelve_hours_other_types_still_work_and_retries_return_original(self):
        _, token = self.employee_with(["BREAKFAST", "LUNCH"])
        first_scan = self.typed_scan(token, "BREAKFAST")
        first = self.locked_meals.record(self.waiter_context, first_scan)
        self.assertTrue(first.approved)
        again = self.locked_meals.record(self.waiter_context, self.typed_scan(token, "BREAKFAST"))
        self.assertEqual((again.approved, again.code), (False, "MEAL_TYPE_ALREADY_RECORDED"))
        served = self.scalar("SELECT served_at FROM meals WHERE id = %s", (first.meal_ids[0],))
        self.assertEqual(again.previous_served_at, served.replace(microsecond=0).isoformat() + "Z")
        retry = self.locked_meals.record(self.waiter_context, first_scan)
        self.assertTrue(retry.approved and retry.duplicate)
        self.assertEqual(retry.meal_ids, first.meal_ids)
        self.assertTrue(self.locked_meals.record(self.waiter_context, self.typed_scan(token, "LUNCH")).approved)

    def test_simultaneous_scans_of_one_type_record_exactly_one_meal(self):
        _, token = self.employee_with(["DINNER"])
        barrier = threading.Barrier(3)

        def scan(context):
            barrier.wait(timeout=10)
            return self.locked_meals.record(context, self.typed_scan(token, "DINNER"))

        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(scan, (self.waiter_context, self.waiter_context_two, self.admin_context)))
        self.assertEqual(sum(result.approved for result in results), 1)
        self.assertEqual(sorted(result.code for result in results if not result.approved), ["MEAL_TYPE_ALREADY_RECORDED"] * 2)

    def test_correction_keeps_the_original_and_moves_the_lock_to_the_corrected_type(self):
        from meal_management.errors import DomainError
        from meal_management.queries import QueryService
        from meal_management.reports import ReportService

        _, token = self.employee_with(["BREAKFAST", "LUNCH"])
        recorded = self.locked_meals.record(self.waiter_context, self.typed_scan(token, "BREAKFAST"))
        meal_id = recorded.meal_ids[0]
        with self.assertRaises(DomainError):
            self.meals.correct_meal_type(self.waiter_context, meal_id, self.type_ids["LUNCH"], "Wrong type")
        with self.assertRaisesRegex(DomainError, "INVALID_CORRECTION_REASON"):
            self.meals.correct_meal_type(self.admin_context, meal_id, self.type_ids["LUNCH"], " ")
        with self.assertRaisesRegex(DomainError, "MEAL_TYPE_UNCHANGED"):
            self.meals.correct_meal_type(self.admin_context, meal_id, self.type_ids["BREAKFAST"], "Same type")
        self.meals.correct_meal_type(self.admin_context, meal_id, self.type_ids["LUNCH"], "Waiter picked Breakfast at lunchtime")
        self.assertEqual(self.scalar("SELECT meal_type_id FROM servings WHERE id = %s", (recorded.serving_id,)), self.type_ids["BREAKFAST"])
        self.assertEqual(self.locked_meals.record(self.waiter_context, self.typed_scan(token, "LUNCH")).code, "MEAL_TYPE_ALREADY_RECORDED")
        self.assertTrue(self.locked_meals.record(self.waiter_context, self.typed_scan(token, "BREAKFAST")).approved)
        served = self.scalar("SELECT served_at FROM meals WHERE id = %s", (meal_id,)).replace(tzinfo=timezone.utc)
        history = QueryService(self.db).meal_history(self.admin_context, served - timedelta(seconds=1), served + timedelta(seconds=1), limit=1000, meal_type_id=self.type_ids["LUNCH"])["items"]
        row = next(item for item in history if item["meal_id"] == meal_id)
        self.assertEqual((row["meal_type_id"], row["recorded_meal_type_id"], row["correction_count"]), (self.type_ids["LUNCH"], self.type_ids["BREAKFAST"], 1))
        totals = ReportService(self.db).meal_report(self.admin_context, served - timedelta(seconds=1), served + timedelta(seconds=1))
        by_type = {group["meal_type_id"]: group["meal_count"] for group in totals["by_meal_type"]}
        self.assertGreaterEqual(by_type.get(self.type_ids["LUNCH"], 0), 1)
        actions = [item["action"] for item in self.rows("SELECT action FROM audit_events WHERE entity_type = 'meals' AND entity_id = %s", (str(meal_id),))]
        self.assertEqual(actions, ["MEAL_TYPE_CORRECTED"])
        for sql in ("UPDATE meal_type_corrections SET reason = 'x'", "DELETE FROM meal_type_corrections"):
            cursor = self.admin_connection.cursor()
            try:
                with self.assertRaises(Exception) as caught:
                    cursor.execute(sql + " WHERE meal_id = %s", (meal_id,))
                self.assertEqual(getattr(caught.exception, "errno", None), 1644)
            finally:
                cursor.close()

    def test_visitors_use_the_selected_type_without_eligibility_or_lock(self):
        from meal_management.scan_app import ScanAppService
        from meal_management.scan_receipts import ScanReceiptService

        credential = self.qr.issue_master(self.admin_context, company_name="Visitors", contact_name="Lead",
                                          email="visitors@example.test", phone="+91 98765 43210", meal_limit=3,
                                          meal_type_ids=[self.type_ids["DINNER"]])
        shared = ScanAppService(self.db, self.locked_meals, ScanReceiptService(self.db))
        browser = secrets.token_bytes(32)
        results = [shared.read(browser, uuid4(), credential.token, self.type_ids["DINNER"]) for _ in range(3)]
        self.assertTrue(all(result.approved for result in results))
        types = {row["meal_type_id"] for row in self.rows("SELECT meal_type_id FROM servings WHERE qr_id = %s", (credential.qr_id,))}
        self.assertEqual(types, {self.type_ids["DINNER"]})
        self.assertEqual(shared.read(browser, uuid4(), credential.token, self.type_ids["DINNER"]).code, "QR_EXPIRED")

    def test_public_scanner_retry_keeps_the_type_it_started_with(self):
        from meal_management.scan_app import ScanAppService
        from meal_management.scan_receipts import ScanReceiptService

        _, token = self.employee_with(["LUNCH", "DINNER"])
        shared = ScanAppService(self.db, self.locked_meals, ScanReceiptService(self.db))
        browser, request = secrets.token_bytes(32), uuid4()
        first = shared.read(browser, request, token, self.type_ids["LUNCH"])
        retry = shared.read(browser, request, token, self.type_ids["DINNER"])
        self.assertTrue(first.approved and retry.duplicate)
        self.assertEqual(self.scalar("SELECT meal_type_id FROM servings WHERE id = %s", (first.serving_id,)), self.type_ids["LUNCH"])
        self.assertEqual(shared.meal_types()["meal_types"][0].keys(), {"id", "code", "name"})
        self.assertNotIn("SCANNER-MEAL", [row["code"] for row in shared.meal_types()["meal_types"]])


class MealTypeMigrationDefaultsMySQLTests(MealServicesFixture):
    def test_migration_grants_existing_employees_every_active_type(self):
        from pathlib import Path

        from meal_management.migrations import load_migrations

        registration, _ = self.employee()
        cursor = self.admin_connection.cursor()
        try:
            cursor.execute("DELETE FROM employee_meal_types WHERE employee_id = %s", (registration.employee_id,))
            migration = load_migrations(Path(__file__).resolve().parents[2] / "database" / "migrations")[11]
            statement = next(item for item in migration.statements if item.startswith("INSERT INTO employee_meal_types"))
            cursor.execute(statement.rstrip(";") + " AND e.id = %s", (registration.employee_id,))
        finally:
            cursor.close()
        granted = {row["meal_type_id"] for row in self.rows("SELECT meal_type_id FROM employee_meal_types WHERE employee_id = %s", (registration.employee_id,))}
        self.assertEqual(granted, {row["id"] for row in self.rows("SELECT id FROM meal_types WHERE is_active = 1")})
