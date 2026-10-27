import secrets
from datetime import date, timedelta, timezone
from uuid import uuid4

from test_mysql_services import MealServicesFixture


class ScannerWithoutShiftsMySQLTests(MealServicesFixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from meal_management.queries import QueryService
        from meal_management.reports import ReportService
        from meal_management.scan_app import ScanAppService
        from meal_management.scan_receipts import ScanReceiptService
        from meal_management.scanner_phones import ScannerPhoneService

        cls.queries = QueryService(cls.db)
        cls.reports = ReportService(cls.db)
        cls.shared = ScanAppService(cls.db, cls.meals, ScanReceiptService(cls.db))
        cls.phones = ScannerPhoneService(cls.db, secrets.token_bytes(32))

    def activated_phone(self, label):
        browser = secrets.token_bytes(32)
        code = self.phones.create_code(self.admin_context, label, date.today() + timedelta(days=30))["code"]
        phone = self.phones.activate_code(code, browser)
        return phone["id"], browser

    def window(self):
        now = self.rows("SELECT UTC_TIMESTAMP(6) AS now")[0]["now"].replace(tzinfo=timezone.utc)
        return now - timedelta(days=1), now + timedelta(days=1)

    def meal_rows(self, meal_ids):
        start, end = self.window()
        items = self.queries.meal_history(self.admin_context, start, end, limit=1000)["items"]
        return {item["meal_id"]: item for item in items if item["meal_id"] in set(meal_ids)}

    def copy(self, table, where, params, overrides):
        columns = [row["name"] for row in self.rows(
            "SELECT COLUMN_NAME AS name FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() "
            "AND TABLE_NAME = %s AND EXTRA NOT LIKE '%%auto_increment%%' ORDER BY ORDINAL_POSITION", (table,))]
        selected = ", ".join("%s" if column in overrides else f"`{column}`" for column in columns)
        values = [overrides[column] for column in columns if column in overrides]
        cursor = self.admin_connection.cursor()
        try:
            cursor.execute(
                f"INSERT INTO `{table}` ({', '.join(f'`{column}`' for column in columns)}) "
                f"SELECT {selected} FROM `{table}` WHERE {where}", (*values, *params))
            return cursor.lastrowid
        finally:
            cursor.close()

    def test_a_phone_scans_right_after_activation_and_its_label_is_the_waiter(self):
        phone_id, browser = self.activated_phone("Annapurna Caterers")
        _, token = self.employee()
        result = self.shared.read(browser, uuid4(), token, self.meal_type_id, phone_id=phone_id)
        self.assertTrue(result.approved, result.code)
        serving = self.rows("SELECT waiter_shift_id FROM servings WHERE id = %s", (result.serving_id,))[0]
        self.assertIsNone(serving["waiter_shift_id"])
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM waiter_shifts WHERE phone_id = %s", (phone_id,)), 0)
        row = self.meal_rows(result.meal_ids)[result.meal_ids[0]]
        self.assertEqual((row["waiter_name"], row["scanner_phone_id"]), ("Annapurna Caterers", phone_id))

    def test_report_waiter_totals_show_the_phone_label(self):
        phone_id, browser = self.activated_phone("Counter 2 Snacks")
        _, token = self.employee()
        approved = self.shared.read(browser, uuid4(), token, self.meal_type_id, phone_id=phone_id)
        self.assertTrue(approved.approved)
        start, end = self.window()
        linked = self.shared.read(browser, uuid4(), token, self.meal_type_id, phone_id=phone_id)
        self.assertTrue(linked.approved)
        report = self.reports.meal_report(self.admin_context, start, end)
        waiters = {item["name"]: item["meal_count"] for item in report["by_waiter"]}
        self.assertGreaterEqual(waiters.get("Counter 2 Snacks", 0), 2)
        self.assertTrue(all(isinstance(item["waiter_id"], int) for item in report["by_waiter"]))

    def test_rejected_scan_with_a_request_shows_the_phone_label(self):
        phone_id, browser = self.activated_phone("Rejection Counter")
        lunch = self.scalar("SELECT id FROM meal_types WHERE code = 'LUNCH'")
        registration, token = self.employee()
        self.employees.set_meal_types(self.admin_context, registration.employee_id, [self.meal_type_id])
        refused = self.shared.read(browser, uuid4(), token, lunch, phone_id=phone_id)
        self.assertEqual(refused.code, "MEAL_TYPE_NOT_ELIGIBLE")
        start, end = self.window()
        rejected = self.queries.scans(self.admin_context, start, end, outcome="REJECTED", limit=1000)["items"]
        mine = [item for item in rejected if item["request_id"] == refused.request_id]
        self.assertEqual(len(mine), 1)
        self.assertEqual((mine[0]["waiter_name"], mine[0]["scanner_phone_id"]), ("Rejection Counter", phone_id))

    def test_meals_recorded_under_a_shift_still_display_with_their_phone(self):
        phone_id, browser = self.activated_phone("Earlier Vendor")
        _, token = self.employee()
        original = self.shared.read(browser, uuid4(), token, self.meal_type_id, phone_id=phone_id)
        cursor = self.admin_connection.cursor()
        try:
            cursor.execute(
                "INSERT INTO waiter_shifts (phone_id, waiter_name, selfie_object_key, latitude, longitude, accuracy_m, "
                "consent_notice_version, consent_at, started_at, expires_at) VALUES (%s, 'Ravi', %s, 18.5, 73.8, 10, "
                "'2026-10-26', UTC_TIMESTAMP(6), UTC_TIMESTAMP(6), UTC_TIMESTAMP(6) + INTERVAL 3 HOUR)",
                (phone_id, "shift-selfies/" + "a" * 64 + ".jpg"),
            )
            shift_id = cursor.lastrowid
        finally:
            cursor.close()
        request = uuid4().bytes
        old = self.rows("SELECT request_id FROM servings WHERE id = %s", (original.serving_id,))[0]["request_id"]
        self.copy("serving_requests", "id = %s", (old,), {"id": request})
        self.copy("scan_app_requests", "request_id = %s", (old,), {"request_id": request})
        serving_id = self.copy("servings", "id = %s", (original.serving_id,), {"request_id": request, "waiter_shift_id": shift_id})
        meal_id = self.copy("meals", "serving_id = %s", (original.serving_id,), {"serving_id": serving_id})
        rows = self.meal_rows([meal_id])
        self.assertEqual(self.scalar("SELECT waiter_shift_id FROM servings WHERE id = %s", (serving_id,)), shift_id)
        self.assertEqual(rows[meal_id]["waiter_name"], "Earlier Vendor")

    def test_meals_without_a_phone_fall_back_to_the_scanner_account(self):
        _, token = self.employee()
        result = self.meals.record(self.waiter_context, self.scan(token))
        self.assertTrue(result.approved)
        row = self.meal_rows(result.meal_ids)[result.meal_ids[0]]
        self.assertEqual(row["waiter_name"], "Test Waiter")
        self.assertIsNone(row["scanner_phone_id"])
