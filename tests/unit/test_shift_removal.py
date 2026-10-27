import secrets
import unittest
from dataclasses import replace
from unittest.mock import Mock
from uuid import uuid4

from meal_management.models import ScanResult
from meal_management.queries import QueryService
from meal_management.reports import ReportService
from meal_management.runtime import RuntimeSettings
from meal_management.scan_app import ScanAppService
from test_api import ApiHarness, ApiTestCase
from test_runtime_storage import environment


class ProductionScannerWithoutShiftsTests(ApiTestCase):
    def production(self):
        base = ApiHarness(application="scanner").runtime
        runtime = replace(
            base, environment="production", app_origin="https://admin.example.test",
            admin_origin="https://admin.example.test", scanner_origin="https://scanner.example.test",
            allowed_hosts=("admin.example.test", "scanner.example.test"), cookie_secure=True,
            scanner_enabled=True, scanner_activation_secret=secrets.token_urlsafe(32).encode(),
        )
        harness = ApiHarness(runtime, application="scanner")
        self.addCleanup(harness.client.close)
        harness.app.state.scanner = Mock()
        harness.app.state.scanner.read.return_value = ScanResult(True, "APPROVED", str(uuid4()), 25, (31,))
        harness.phones.activate_code.return_value = {"id": 7, "active_until": None}
        harness.phones.authorize.return_value = {"id": 7, "active_until": None}
        return harness

    def headers(self, harness):
        session = harness.client.get("/api/scanner/session")
        return {"Origin": harness.runtime.scanner_origin, "X-CSRF-Token": session.json()["csrf_token"]}

    def test_scanning_works_right_after_activation_with_no_shift(self):
        harness = self.production()
        activated = harness.client.post("/api/scanner/activate", json={"activation_code": "ABCD-EFGH"},
                                        headers={"Origin": harness.runtime.scanner_origin})
        self.assertEqual(activated.status_code, 200)
        response = harness.client.post("/api/scanner/read", headers=self.headers(harness),
                                       json={"request_id": str(uuid4()), "token": "t" * 43, "meal_type_id": 3})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["approved"])
        call = harness.app.state.scanner.read.call_args
        self.assertEqual(call.kwargs, {"phone_id": 7})
        self.assertEqual(call.args[3], 3)

    def test_shift_status_is_a_harmless_stub_and_shift_changes_are_gone(self):
        harness = self.production()
        harness.client.post("/api/scanner/activate", json={"activation_code": "ABCD-EFGH"},
                            headers={"Origin": harness.runtime.scanner_origin})
        self.assertEqual(harness.client.get("/api/scanner/shift").json(), {"required": False, "shift": None})
        self.assertIn(harness.client.post("/api/scanner/shift", headers=self.headers(harness), json={}).status_code, {404, 405})
        self.assertEqual(harness.client.post("/api/scanner/shift/end", headers=self.headers(harness)).status_code, 404)
        self.client.get("/api/scanner/session")
        self.assertEqual(self.client.get("/api/scanner/shift").json(), {"required": False, "shift": None})

    def test_scanner_and_admin_refuse_location_but_keep_camera(self):
        for harness in (self.production(), self.harness, ApiHarness()):
            with self.subTest(app=harness.runtime.environment):
                policy = harness.client.get("/health/live").headers["permissions-policy"]
                self.assertIn("geolocation=()", policy)
                self.assertIn("camera=(self)", policy)
                self.assertNotIn("geolocation=(self)", policy)

    def setUp(self):
        self.harness = ApiHarness(application="scanner")
        self.client = self.harness.client
        self.addCleanup(self.client.close)


class AdminWithoutShiftsTests(ApiTestCase):
    def test_waiter_shift_pages_and_canteen_settings_are_gone(self):
        self.harness.login()
        for method, path in (("get", "/api/waiter-shifts"), ("get", "/api/waiter-shifts/1/selfie"),
                             ("post", "/api/waiter-shifts/1/end"), ("get", "/api/waiter-shift-settings"),
                             ("put", "/api/waiter-shift-settings")):
            with self.subTest(path=path):
                kwargs = {"headers": self.harness.headers()} if method != "get" else {}
                self.assertEqual(getattr(self.client, method)(path, **kwargs).status_code, 404)
        self.assertFalse(hasattr(self.harness.app.state, "waiter_shifts"))

    def test_shift_hours_setting_is_no_longer_read(self):
        settings = RuntimeSettings.from_env(environment(WAITER_SHIFT_HOURS="999"))
        self.assertFalse(hasattr(settings, "waiter_shift_hours"))

    def test_photo_storage_no_longer_accepts_selfie_keys(self):
        import inspect

        from meal_management.storage import LocalFileStorage, S3Storage, VercelBlobStorage, _photo_key
        from meal_management.errors import DomainError

        for storage in (LocalFileStorage, S3Storage, VercelBlobStorage):
            self.assertNotIn("prefix", inspect.signature(storage.put).parameters)
        with self.assertRaisesRegex(DomainError, "INVALID_PHOTO_KEY"):
            _photo_key("shift-selfies/" + "a" * 64 + ".jpg")
        self.assertEqual(_photo_key("a" * 64 + ".jpg"), "a" * 64 + ".jpg")

    def test_scan_app_records_no_shift(self):
        meals = Mock()
        service = ScanAppService(Mock(), meals, Mock())
        service._reserve = Mock(return_value=(uuid4(), {"staff_id": 4}))
        service._scan = Mock(return_value="scan")
        service.read(b"b" * 32, uuid4(), "token", 3, phone_id=7)
        context = meals.record.call_args.args[0]
        self.assertEqual((context.staff_id, context.phone_id), (4, 7))
        self.assertFalse(hasattr(context, "waiter_shift_id"))


class WaiterLabelSqlTests(unittest.TestCase):
    def test_meal_records_rejected_scans_and_totals_use_the_phone_label(self):
        from datetime import datetime, timezone
        from unittest.mock import MagicMock, patch

        from meal_management.models import Actor
        from meal_management.security import generate_token
        from meal_management.models import ServerContext

        db = MagicMock()
        tx = db.transaction.return_value.__enter__.return_value
        tx.all.return_value = []
        start = datetime(2026, 10, 1, tzinfo=timezone.utc)
        end = datetime(2026, 10, 2, tzinfo=timezone.utc)
        context = ServerContext(generate_token())
        join = ("LEFT JOIN scan_app_requests ar ON ar.request_id = {} "
                "LEFT JOIN scanner_phones ph ON ph.browser_hash = ar.browser_hash")
        with patch("meal_management.queries.require_actor", return_value=Actor(1, frozenset({"ADMIN"}))):
            QueryService(db).meal_history(context, start, end)
            sql = tx.all.call_args.args[0]
            self.assertIn("COALESCE(ph.label, w.display_name) AS waiter_name", sql)
            self.assertIn(join.format("s.request_id"), sql)
            QueryService(db).scans(context, start, end, outcome="REJECTED")
            sql = tx.all.call_args.args[0]
            self.assertIn("COALESCE(ph.label, s.display_name) AS waiter_name", sql)
            self.assertIn(join.format("a.request_id"), sql)
        with patch("meal_management.reports.require_actor", return_value=Actor(1, frozenset({"ADMIN"}))):
            ReportService(db).meal_report(context, start, end)
            sql = tx.all.call_args.args[0]
            self.assertIn("COALESCE(ph.label, w.display_name) AS waiter_name", sql)
            self.assertIn(join.format("s.request_id"), sql)
            self.assertIn("GROUP BY waiter_id, waiter_name", sql)
        for statement in (sql,):
            self.assertNotIn("waiter_shifts", statement)


if __name__ == "__main__":
    unittest.main()
