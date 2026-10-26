import base64
import io
from pathlib import Path
import secrets
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

from test_mysql_services import MealServicesFixture

CANTEEN = (18.520430, 73.856743)


def selfie_base64():
    from PIL import Image

    output = io.BytesIO()
    Image.new("RGB", (64, 64), "teal").save(output, format="JPEG")
    return base64.b64encode(output.getvalue()).decode()


class WaiterShiftMySQLTests(MealServicesFixture):
    def setUp(self):
        from meal_management.scanner_phones import ScannerPhoneService
        from meal_management.storage import LocalFileStorage
        from meal_management.waiter_shifts import WaiterShiftService

        super().setUp()
        self.photo_root = tempfile.TemporaryDirectory()
        self.addCleanup(self.photo_root.cleanup)
        self.storage = LocalFileStorage(str(Path(self.photo_root.name).resolve() / "photos"))
        self.shifts = WaiterShiftService(self.db, self.storage, shift_hours=3)
        self.phone = ScannerPhoneService(self.db, secrets.token_bytes(32)).register_secret_activation(secrets.token_bytes(32))["id"]

    def start(self, name="Ravi", latitude=CANTEEN[0], longitude=CANTEEN[1], accuracy=12.5, phone=None):
        from meal_management.waiter_shifts import NOTICE_VERSION

        return self.shifts.start(phone or self.phone, name, selfie_base64(), latitude, longitude, accuracy, True, NOTICE_VERSION)

    def shift_row(self, shift_id):
        return self.rows("SELECT * FROM waiter_shifts WHERE id = %s", (shift_id,))[0]

    def admin_errno(self, sql, params=()):
        cursor = self.admin_connection.cursor()
        try:
            cursor.execute(sql, params)
        except Exception as error:
            return getattr(error, "errno", None)
        finally:
            cursor.close()
        return None

    def test_sign_in_records_name_selfie_location_accuracy_and_consent(self):
        from meal_management.waiter_shifts import NOTICE_VERSION

        shift = self.start()
        row = self.shift_row(shift["id"])
        self.assertEqual((row["waiter_name"], float(row["accuracy_m"]), row["consent_notice_version"]), ("Ravi", 12.5, NOTICE_VERSION))
        self.assertTrue(row["selfie_object_key"].startswith("shift-selfies/"))
        self.assertEqual(row["consent_at"], row["started_at"])
        self.assertEqual(row["expires_at"] - row["started_at"], timedelta(hours=3))
        self.assertIsNone(row["flagged"])
        data, content_type = self.shifts.selfie(self.admin_context, shift["id"])
        self.assertEqual(content_type, "image/jpeg")
        self.assertGreater(len(data), 100)
        self.assertEqual(self.shifts.require(self.phone), shift["id"])
        actions = [item["action"] for item in self.rows("SELECT action FROM audit_events WHERE entity_type = 'waiter_shifts' AND entity_id = %s", (str(shift["id"]),))]
        self.assertEqual(actions, ["SHIFT_STARTED"])

    def test_consent_and_location_are_required(self):
        from meal_management.errors import DomainError
        from meal_management.waiter_shifts import NOTICE_VERSION

        for consent, version in ((False, NOTICE_VERSION), (True, "old-notice")):
            with self.assertRaisesRegex(DomainError, "SHIFT_CONSENT_REQUIRED"):
                self.shifts.start(self.phone, "Ravi", selfie_base64(), *CANTEEN, 10.0, consent, version)
        with self.assertRaisesRegex(DomainError, "INVALID_LATITUDE"):
            self.shifts.start(self.phone, "Ravi", selfie_base64(), 95.0, 73.0, 10.0, True, NOTICE_VERSION)
        with self.assertRaisesRegex(DomainError, "INVALID_SHIFT_SELFIE"):
            self.shifts.start(self.phone, "Ravi", "not base64!", *CANTEEN, 10.0, True, NOTICE_VERSION)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM waiter_shifts WHERE phone_id = %s", (self.phone,)), 0)

    def test_sign_ins_outside_the_canteen_radius_are_flagged_but_allowed(self):
        self.shifts.update_settings(self.admin_context, CANTEEN[0], CANTEEN[1], 100)
        inside = self.start(latitude=CANTEEN[0] + 0.0004)
        self.assertEqual(inside["flagged"], False)
        self.assertLess(inside["distance_m"], 100)
        outside = self.start(latitude=CANTEEN[0] + 0.01, accuracy=1500.0)
        self.assertEqual(outside["flagged"], True)
        self.assertGreater(outside["distance_m"], 1000)
        flagged = self.shifts.list(self.admin_context, status="flagged")["items"]
        self.assertIn(outside["id"], [item["id"] for item in flagged])
        self.assertNotIn(inside["id"], [item["id"] for item in flagged])
        self.shifts.update_settings(self.admin_context, None, None, None)
        self.assertFalse(self.shifts.settings(self.admin_context)["configured"])
        audits = [item["action"] for item in self.rows("SELECT action FROM audit_events WHERE entity_type = 'waiter_shift_settings'")]
        self.assertGreaterEqual(audits.count("CANTEEN_LOCATION_UPDATED"), 2)

    def test_new_sign_in_replaces_sign_out_and_admin_end(self):
        from meal_management.errors import DomainError

        first = self.start("Ravi")
        second = self.start("Asha")
        self.assertEqual(self.shift_row(first["id"])["end_reason"], "REPLACED")
        self.assertEqual(self.shifts.require(self.phone), second["id"])
        self.assertTrue(self.shifts.end(self.phone)["ended"])
        self.assertEqual(self.shift_row(second["id"])["end_reason"], "SIGNED_OUT")
        with self.assertRaisesRegex(DomainError, "SHIFT_SIGN_IN_REQUIRED"):
            self.shifts.require(self.phone)
        third = self.start("Meera")
        with self.assertRaises(DomainError):
            self.shifts.admin_end(self.waiter_context, third["id"])
        self.shifts.admin_end(self.admin_context, third["id"])
        row = self.shift_row(third["id"])
        self.assertEqual((row["end_reason"], row["ended_by_staff_id"]), ("ENDED_BY_ADMIN", self.admin_id))
        with self.assertRaisesRegex(DomainError, "SHIFT_ALREADY_ENDED"):
            self.shifts.admin_end(self.admin_context, third["id"])

    def test_expired_shift_requires_a_new_sign_in(self):
        from meal_management.errors import DomainError

        from meal_management.scanner_phones import ScannerPhoneService

        shift = self.start()
        expired_phone = ScannerPhoneService(self.db, secrets.token_bytes(32)).register_secret_activation(secrets.token_bytes(32))["id"]
        cursor = self.admin_connection.cursor()
        try:
            cursor.execute(
                "INSERT INTO waiter_shifts (phone_id, waiter_name, selfie_object_key, latitude, longitude, accuracy_m, "
                "consent_notice_version, consent_at, started_at, expires_at) VALUES "
                "(%s, 'Old', 'shift-selfies/x.jpg', 18.5, 73.8, 5, 'v', UTC_TIMESTAMP(6) - INTERVAL 4 HOUR, "
                "UTC_TIMESTAMP(6) - INTERVAL 4 HOUR, UTC_TIMESTAMP(6) - INTERVAL 1 HOUR)",
                (expired_phone,),
            )
        finally:
            cursor.close()
        with self.assertRaisesRegex(DomainError, "SHIFT_SIGN_IN_REQUIRED"):
            self.shifts.require(expired_phone)
        statuses = {item["id"]: item["status"] for item in self.shifts.list(self.admin_context, limit=200)["items"]}
        self.assertEqual(statuses[shift["id"]], "ACTIVE")
        self.assertIn("EXPIRED", statuses.values())

    def test_simultaneous_sign_ins_on_one_phone_leave_one_open_shift(self):
        barrier = threading.Barrier(3)

        def sign_in(name):
            barrier.wait(timeout=10)
            return self.start(name)["id"]

        with ThreadPoolExecutor(max_workers=3) as pool:
            identifiers = list(pool.map(sign_in, ("A", "B", "C")))
        open_rows = self.rows("SELECT id FROM waiter_shifts WHERE phone_id = %s AND ended_at IS NULL", (self.phone,))
        self.assertEqual(len(open_rows), 1)
        self.assertIn(open_rows[0]["id"], identifiers)

    def test_every_scan_records_the_signed_in_shift(self):
        from meal_management.scan_app import ScanAppService
        from meal_management.scan_receipts import ScanReceiptService

        shift = self.start()
        _, token = self.employee()
        shared = ScanAppService(self.db, self.meals, ScanReceiptService(self.db))
        browser = secrets.token_bytes(32)
        result = shared.read(browser, uuid4(), token, self.meal_type_id, phone_id=self.phone, waiter_shift_id=shift["id"])
        self.assertTrue(result.approved)
        self.assertEqual(self.scalar("SELECT waiter_shift_id FROM servings WHERE id = %s", (result.serving_id,)), shift["id"])
        refused = shared.read(browser, uuid4(), "unknown-token-" + uuid4().hex, self.meal_type_id, phone_id=self.phone, waiter_shift_id=shift["id"])
        self.assertFalse(refused.approved)
        attempts = self.rows("SELECT waiter_shift_id, rejection_code FROM scan_attempts ORDER BY id")
        self.assertEqual([row["rejection_code"] for row in attempts], [None, "INVALID_QR"])
        self.assertEqual({row["waiter_shift_id"] for row in attempts}, {shift["id"]})

    def test_database_blocks_shift_deletes_edits_and_reopening(self):
        shift = self.start()
        for sql in ("UPDATE waiter_shifts SET waiter_name = 'Other' WHERE id = %s",
                    "UPDATE waiter_shifts SET latitude = 0 WHERE id = %s",
                    "UPDATE waiter_shifts SET expires_at = expires_at + INTERVAL 1 DAY WHERE id = %s",
                    "DELETE FROM waiter_shifts WHERE id = %s"):
            with self.subTest(sql=sql):
                self.assertEqual(self.admin_errno(sql, (shift["id"],)), 1644)
        self.shifts.end(self.phone)
        self.assertEqual(self.admin_errno("UPDATE waiter_shifts SET ended_at = NULL, end_reason = NULL WHERE id = %s", (shift["id"],)), 1644)

    def test_selfie_missing_after_retention_is_reported_and_record_stays(self):
        from meal_management.errors import DomainError

        shift = self.start()
        self.storage.delete(self.shift_row(shift["id"])["selfie_object_key"])
        with self.assertRaisesRegex(DomainError, "SHIFT_SELFIE_EXPIRED"):
            self.shifts.selfie(self.admin_context, shift["id"])
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM waiter_shifts WHERE id = %s", (shift["id"],)), 1)
