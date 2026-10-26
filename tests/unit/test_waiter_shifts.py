import base64
import io
import re
import secrets
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, Mock
from uuid import uuid4

from meal_management.api_schemas import CanteenLocationInput, ShiftStartInput
from meal_management.errors import ConfigurationError, DomainError
from meal_management.models import ScanResult
from meal_management.runtime import RuntimeSettings
from meal_management.storage import LocalFileStorage, S3Storage, StoredPhoto
from meal_management.waiter_shifts import (
    MAX_SELFIE_BYTES, NOTICE_VERSION, SELFIE_PREFIX, WaiterShiftService, decode_selfie, distance_m,
)
from test_api import ApiHarness, ApiTestCase
from test_runtime_storage import environment


def jpeg():
    from PIL import Image

    output = io.BytesIO()
    Image.new("RGB", (16, 16), "teal").save(output, format="JPEG")
    return output.getvalue()


def selfie():
    return base64.b64encode(jpeg()).decode()


class ShiftRuleTests(unittest.TestCase):
    def test_distance_uses_great_circle_metres(self):
        self.assertEqual(distance_m(18.5, 73.8, 18.5, 73.8), 0)
        self.assertAlmostEqual(distance_m(0, 0, 1, 0), 111_195, delta=10)
        self.assertAlmostEqual(distance_m(18.520430, 73.856743, 18.520430 + 0.0009, 73.856743), 100, delta=1)

    def test_selfie_must_be_strict_bounded_base64(self):
        self.assertEqual(decode_selfie(base64.b64encode(b"abc").decode()), b"abc")
        for value in (None, "", "not base64!", "YWJj\n", 12, "A" * ((MAX_SELFIE_BYTES * 4) // 3 + 8)):
            with self.subTest(value=str(value)[:20]), self.assertRaisesRegex(DomainError, "INVALID_SHIFT_SELFIE"):
                decode_selfie(value)

    def test_scanner_and_server_use_the_same_notice_version(self):
        source = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "shift_signin.js").read_text()
        versions = re.findall(r'^export const NOTICE_VERSION = "([^"]+)";$', source, re.MULTILINE)
        self.assertEqual(versions, [NOTICE_VERSION])

    def test_shift_hours_are_bounded(self):
        self.assertEqual(RuntimeSettings.from_env(environment()).waiter_shift_hours, 3)
        self.assertEqual(RuntimeSettings.from_env(environment(WAITER_SHIFT_HOURS="12")).waiter_shift_hours, 12)
        for value in ("0", "13", "three", "-1"):
            with self.subTest(value=value), self.assertRaises(ConfigurationError):
                RuntimeSettings.from_env(environment(WAITER_SHIFT_HOURS=value))
        for value in (0, 13, "3", 2.5):
            with self.subTest(value=value), self.assertRaisesRegex(DomainError, "INVALID_SHIFT_HOURS"):
                WaiterShiftService(Mock(), Mock(), value)


class ShiftServiceTests(unittest.TestCase):
    def setUp(self):
        self.database = MagicMock()
        self.storage = Mock()
        self.storage.put.return_value = StoredPhoto(SELFIE_PREFIX + "a" * 64 + ".jpg", "image/jpeg", 10)
        self.service = WaiterShiftService(self.database, self.storage)

    def start(self, **changes):
        values = {"phone_id": 1, "waiter_name": "Ravi", "selfie": selfie(), "latitude": 18.5, "longitude": 73.8,
                  "accuracy_m": 2500.0, "consent": True, "notice_version": NOTICE_VERSION}
        values.update(changes)
        return self.service.start(**values)

    def test_consent_and_inputs_are_checked_before_anything_is_stored(self):
        for changes, code in (
            ({"consent": False}, "SHIFT_CONSENT_REQUIRED"),
            ({"consent": "yes"}, "SHIFT_CONSENT_REQUIRED"),
            ({"notice_version": "2020-01-01"}, "SHIFT_CONSENT_REQUIRED"),
            ({"waiter_name": "  "}, "WAITER_NAME"),
            ({"latitude": 91.0}, "INVALID_LATITUDE"),
            ({"longitude": float("nan")}, "INVALID_LONGITUDE"),
            ({"accuracy_m": -1.0}, "INVALID_LOCATION_ACCURACY"),
            ({"latitude": True}, "INVALID_LATITUDE"),
            ({"selfie": "%%%"}, "INVALID_SHIFT_SELFIE"),
            ({"phone_id": None}, "INVALID_SCANNER_PHONE_ID"),
        ):
            with self.subTest(changes=changes), self.assertRaisesRegex(DomainError, code):
                self.start(**changes)
        self.storage.put.assert_not_called()
        self.database.transaction.assert_not_called()

    def test_selfie_goes_to_the_private_prefix_and_is_removed_if_recording_fails(self):
        self.service._record_start = Mock(side_effect=DomainError("SCANNER_ACTIVATION_REQUIRED"))
        with self.assertRaisesRegex(DomainError, "SCANNER_ACTIVATION_REQUIRED"):
            self.start()
        self.assertEqual(self.storage.put.call_args.kwargs, {"prefix": SELFIE_PREFIX})
        self.storage.delete.assert_called_once_with(SELFIE_PREFIX + "a" * 64 + ".jpg")

    def test_poor_accuracy_never_blocks_sign_in(self):
        self.service._record_start = Mock(return_value={"id": 4})
        self.assertEqual(self.start(accuracy_m=900_000.0), {"id": 4})
        self.storage.delete.assert_not_called()

    def test_admin_filters_and_pages_are_validated(self):
        for kwargs, code in (({"status": "open"}, "INVALID_SHIFT_FILTER"), ({"limit": 0}, "INVALID_SHIFT_PAGE"),
                             ({"limit": 201}, "INVALID_SHIFT_PAGE"), ({"before": "1.0"}, "INVALID_REPORT_CURSOR")):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(DomainError, code):
                self.service.list(Mock(), **kwargs)

    def test_canteen_location_must_be_complete_or_cleared(self):
        for values, code in (((18.5, None, 100), "INVALID_LONGITUDE"), ((18.5, 73.8, 5), "INVALID_CANTEEN_RADIUS"),
                             ((18.5, 73.8, 100.5), "INVALID_CANTEEN_RADIUS"), ((None, 73.8, 100), "INVALID_LATITUDE")):
            with self.subTest(values=values), self.assertRaisesRegex(DomainError, code):
                self.service.update_settings(Mock(), *values)
        self.database.transaction.assert_not_called()


class SelfieStorageTests(unittest.TestCase):
    def test_local_storage_keeps_selfies_under_their_own_prefix(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = LocalFileStorage(str(Path(folder).resolve() / "photos"))
            stored = storage.put(jpeg(), "image/jpeg", prefix=SELFIE_PREFIX)
            self.assertRegex(stored.key, r"^shift-selfies/[0-9a-f]{64}\.jpg$")
            self.assertEqual(storage.read(stored.key)[1], "image/jpeg")
            plain = storage.put(jpeg(), "image/jpeg")
            self.assertRegex(plain.key, r"^[0-9a-f]{64}\.jpg$")
            with self.assertRaisesRegex(DomainError, "INVALID_PHOTO_PREFIX"):
                storage.put(jpeg(), "image/jpeg", prefix="../")
            for key in ("other/" + "a" * 64 + ".jpg", "shift-selfies/../" + "a" * 64 + ".jpg"):
                with self.subTest(key=key), self.assertRaisesRegex(DomainError, "INVALID_PHOTO_KEY"):
                    storage.read(key)

    def test_s3_selfie_keys_use_the_lifecycle_prefix(self):
        client = Mock()
        stored = S3Storage("bucket", "eu-north-1", client=client).put(jpeg(), "image/jpeg", prefix=SELFIE_PREFIX)
        self.assertTrue(client.put_object.call_args.kwargs["Key"].startswith("shift-selfies/"))
        self.assertEqual(client.put_object.call_args.kwargs["Key"], stored.key)


class ShiftSchemaTests(unittest.TestCase):
    def test_canteen_input_rejects_partial_locations(self):
        self.assertEqual(CanteenLocationInput().latitude, None)
        CanteenLocationInput(latitude=18.5, longitude=73.8, radius_m=100)
        for values in ({"latitude": 18.5}, {"latitude": 18.5, "longitude": 73.8}, {"radius_m": 100}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                CanteenLocationInput(**values)

    def test_shift_input_requires_strict_consent(self):
        values = {"waiter_name": "Ravi", "latitude": 1.0, "longitude": 1.0, "accuracy_m": 5.0,
                  "notice_version": NOTICE_VERSION, "selfie": "YWJj"}
        self.assertTrue(ShiftStartInput(**values, consent=True).consent)
        for consent in ("true", 1):
            with self.subTest(consent=consent), self.assertRaises(ValueError):
                ShiftStartInput(**values, consent=consent)


class ScannerShiftApiTests(ApiTestCase):
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
        response = harness.client.post("/api/scanner/activate", json={"activation_code": "ABCD-EFGH"},
                                       headers={"Origin": runtime.scanner_origin})
        self.assertEqual(response.status_code, 200, response.text)
        harness.phones.authorize.return_value = {"id": 1, "active_until": None}
        return harness

    def headers(self, harness):
        session = harness.client.get("/api/scanner/session")
        return {"Origin": harness.runtime.scanner_origin, "X-CSRF-Token": session.json().get("csrf_token", "")}

    def read(self, harness):
        return harness.client.post("/api/scanner/read", headers=self.headers(harness),
                                   json={"request_id": str(uuid4()), "token": "t" * 43, "meal_type_id": 5})

    def body(self, **changes):
        values = {"waiter_name": "Ravi", "latitude": 18.5, "longitude": 73.8, "accuracy_m": 30.0,
                  "consent": True, "notice_version": NOTICE_VERSION, "selfie": selfie()}
        values.update(changes)
        return values

    def test_production_scan_requires_a_signed_in_shift_and_records_it(self):
        harness = self.production()
        harness.shifts.require.side_effect = DomainError("SHIFT_SIGN_IN_REQUIRED")
        response = self.read(harness)
        self.assert_error(response, 403, "SHIFT_SIGN_IN_REQUIRED")
        self.assertIn("Sign in", response.json()["error"]["message"])
        harness.app.state.scanner.read.assert_not_called()
        harness.shifts.require.side_effect = None
        harness.shifts.require.return_value = 11
        self.assertEqual(self.read(harness).status_code, 200)
        self.assertEqual(harness.app.state.scanner.read.call_args.kwargs["waiter_shift_id"], 11)
        self.assertEqual(harness.shifts.require.call_args.args, (1,))

    def test_shift_status_sign_in_and_sign_out(self):
        harness = self.production()
        harness.shifts.current.return_value = {"id": 11, "waiter_name": "Ravi"}
        response = harness.client.get("/api/scanner/shift")
        self.assertEqual(response.json(), {"required": True, "shift": {"id": 11, "waiter_name": "Ravi"},
                                           "notice_version": NOTICE_VERSION})
        harness.shifts.start.return_value = {"id": 12}
        response = harness.client.post("/api/scanner/shift", json=self.body(), headers=self.headers(harness))
        self.assertEqual(response.status_code, 201, response.text)
        args = harness.shifts.start.call_args.args
        self.assertEqual(args[:2] + args[3:], (1, "Ravi", 18.5, 73.8, 30.0, True, NOTICE_VERSION))
        harness.shifts.end.return_value = {"ended": True, "shift_id": 12}
        response = harness.client.post("/api/scanner/shift/end", headers=self.headers(harness))
        self.assertEqual(response.json(), {"ended": True, "shift_id": 12})

    def test_sign_in_input_errors_and_consent_refusal(self):
        harness = self.production()
        for body in (self.body(consent="true"), self.body(latitude=91), self.body(waiter_name=""),
                     {key: value for key, value in self.body().items() if key != "selfie"}):
            with self.subTest(body={key: str(value)[:10] for key, value in body.items()}):
                response = harness.client.post("/api/scanner/shift", json=body, headers=self.headers(harness))
                self.assert_error(response, 422, "INVALID_INPUT")
        harness.shifts.start.assert_not_called()
        harness.shifts.start.side_effect = DomainError("SHIFT_CONSENT_REQUIRED")
        response = harness.client.post("/api/scanner/shift", json=self.body(), headers=self.headers(harness))
        self.assert_error(response, 403, "SHIFT_CONSENT_REQUIRED")

    def test_sign_in_needs_csrf(self):
        harness = self.production()
        response = harness.client.post("/api/scanner/shift", json=self.body(),
                                       headers={"Origin": harness.runtime.scanner_origin})
        self.assertEqual(response.status_code, 403)
        harness.shifts.start.assert_not_called()

    def test_selfie_body_limit_is_larger_only_for_sign_in(self):
        harness = self.production()
        big = self.body(selfie="A" * 200_000)
        harness.shifts.start.return_value = {"id": 12}
        response = harness.client.post("/api/scanner/shift", json=big, headers=self.headers(harness))
        self.assertEqual(response.status_code, 201, response.text)
        response = harness.client.post("/api/scanner/read", headers=self.headers(harness),
                                       json={"request_id": str(uuid4()), "token": "t" * 200_000, "meal_type_id": 5})
        self.assertEqual(response.status_code, 413)
        response = harness.client.post("/api/scanner/shift", json=self.body(selfie="A" * 1_600_000),
                                       headers=self.headers(harness))
        self.assertEqual(response.status_code, 413)

    def test_scanner_allows_geolocation_for_its_own_origin(self):
        harness = self.production()
        policy = harness.client.get("/api/scanner/session").headers["permissions-policy"]
        self.assertIn("geolocation=(self)", policy)
        self.assertIn("camera=(self)", policy)

    def test_development_scanner_needs_no_shift(self):
        self.client.get("/api/scanner/session")
        response = self.client.get("/api/scanner/shift")
        self.assertEqual(response.json(), {"required": False, "shift": None})
        self.harness.shifts.require.assert_not_called()
        self.harness.shifts.current.assert_not_called()

    def setUp(self):
        self.harness = ApiHarness(application="scanner")
        self.client = self.harness.client
        self.addCleanup(self.client.close)


class AdminShiftApiTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.shifts = self.harness.shifts
        self.shifts.list.return_value = {"items": [], "next_cursor": None}
        self.shifts.selfie.return_value = (jpeg(), "image/jpeg")
        self.shifts.admin_end.return_value = {"id": 4, "ended": True}
        self.shifts.settings.return_value = {"configured": False, "latitude": None, "longitude": None,
                                             "radius_m": None, "notice_version": NOTICE_VERSION}
        self.shifts.update_settings.return_value = {"configured": True}

    def test_admin_lists_views_selfie_ends_shift_and_sets_canteen(self):
        self.harness.login()
        self.assertEqual(self.client.get("/api/waiter-shifts?status=flagged&limit=20").status_code, 200)
        self.assertEqual(self.shifts.list.call_args.kwargs, {"status": "flagged", "limit": 20, "before": None})
        response = self.client.get("/api/waiter-shifts/4/selfie")
        self.assertEqual((response.status_code, response.headers["content-type"]), (200, "image/jpeg"))
        self.assertIn("no-store", response.headers["cache-control"])
        self.assertEqual(self.harness.post("/api/waiter-shifts/4/end").json(), {"id": 4, "ended": True})
        self.assertEqual(self.shifts.admin_end.call_args.args[1], 4)
        self.assertEqual(self.client.get("/api/waiter-shift-settings").status_code, 200)
        response = self.client.put("/api/waiter-shift-settings", headers=self.harness.headers(),
                                   json={"latitude": 18.5, "longitude": 73.8, "radius_m": 100})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.shifts.update_settings.call_args.args[1:], (18.5, 73.8, 100))

    def test_partial_canteen_location_is_rejected(self):
        self.harness.login()
        response = self.client.put("/api/waiter-shift-settings", headers=self.harness.headers(),
                                   json={"latitude": 18.5, "longitude": 73.8})
        self.assert_error(response, 422, "INVALID_INPUT")
        self.shifts.update_settings.assert_not_called()

    def test_expired_selfie_and_finished_shift_errors(self):
        self.harness.login()
        self.shifts.selfie.side_effect = DomainError("SHIFT_SELFIE_EXPIRED")
        response = self.client.get("/api/waiter-shifts/4/selfie")
        self.assertEqual(response.status_code, 404)
        self.assertIn("90 days", response.json()["error"]["message"])
        self.shifts.admin_end.side_effect = DomainError("SHIFT_ALREADY_ENDED")
        self.assert_error(self.harness.post("/api/waiter-shifts/4/end"), 409, "SHIFT_ALREADY_ENDED")

    def test_waiters_and_anonymous_users_cannot_see_shifts(self):
        calls = (
            lambda: self.client.get("/api/waiter-shifts"),
            lambda: self.client.get("/api/waiter-shifts/4/selfie"),
            lambda: self.harness.post("/api/waiter-shifts/4/end"),
            lambda: self.client.get("/api/waiter-shift-settings"),
            lambda: self.client.put("/api/waiter-shift-settings", headers=self.harness.headers(), json={}),
        )
        for call in calls:
            self.assert_error(call(), 401, "AUTHENTICATION_REQUIRED")
        self.harness.login("waiter")
        for call in calls:
            self.assert_error(call(), 403, "ROLE_REQUIRED")
        for method in (self.shifts.list, self.shifts.selfie, self.shifts.admin_end, self.shifts.settings,
                       self.shifts.update_settings):
            method.assert_not_called()


if __name__ == "__main__":
    unittest.main()
