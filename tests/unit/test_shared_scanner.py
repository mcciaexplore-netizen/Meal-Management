import unittest
from unittest.mock import MagicMock, Mock
from uuid import uuid4

from meal_management.errors import DomainError
from meal_management.models import ScanAppContext, ScanResult
from meal_management.scan_app import ScanAppService
from meal_management.security import generate_token


class SharedScannerTests(unittest.TestCase):
    def setUp(self):
        self.db = MagicMock()
        self.tx = self.db.transaction.return_value.__enter__.return_value
        self.meals = Mock()
        self.receipts = Mock()
        self.service = ScanAppService(self.db, self.meals, self.receipts)
        self.browser = b"b" * 32
        self.identifier = uuid4()
        self.token = generate_token()
        self.profile = {
            "staff_id": 42, "scanner_id": 3, "meal_type_id": 7, "scanner_code": "SHARED-SCANNER",
            "is_enabled": True, "staff_active": True, "is_scanner": True,
            "scanner_active": True, "location_active": True, "meal_type_active": True,
        }
        self.mapping = {
            "request_id": self.identifier.bytes, "browser_hash": self.browser,
            "staff_id": 42, "scanner_id": 3, "meal_type_id": 7, "scanner_code": "SHARED-SCANNER",
        }
        self.details = {"company_name": "Example Ltd", "name": "Ravi Patel", "email": "ravi@example.test", "phone": "+919876543210"}

    def assert_domain(self, code, action):
        with self.assertRaises(DomainError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)

    def test_constructor_does_not_connect_or_create_staff_sessions(self):
        self.db.transaction.assert_not_called()
        self.meals.assert_not_called()
        self.receipts.assert_not_called()

    def test_read_captures_defaults_and_uses_dedicated_actor_without_login(self):
        self.tx.one.side_effect = [None, self.profile]
        expected = ScanResult(True, "APPROVED", str(self.identifier), 77, (91,))
        self.meals.record.return_value = expected
        actual = self.service.read(self.browser, self.identifier, self.token)
        self.assertIs(actual, expected)
        context, scan = self.meals.record.call_args.args
        self.assertIsInstance(context, ScanAppContext)
        self.assertEqual(context.staff_id, 42)
        self.assertEqual(scan.request_id, self.identifier)
        self.assertEqual(scan.token, self.token)
        self.assertEqual(scan.scanner_code, "SHARED-SCANNER")
        self.assertEqual(scan.meal_type_id, 7)
        self.assertEqual(scan.quantity, 1)
        self.assertIsNone(scan.authorization_id)
        self.assertIsNone(scan.visitor_details)
        insert = self.tx.execute.call_args
        self.assertEqual(insert.args[1], (self.identifier.bytes, self.browser, 42, 3, 7, "SHARED-SCANNER"))
        self.assertNotIn("token", insert.args[0])
        self.assertNotIn(self.token, repr(insert))
        self.assertFalse(any("staff_sessions" in call.args[0] for call in self.tx.one.call_args_list))

    def test_master_scan_records_through_the_same_transactional_service(self):
        self.tx.one.return_value = self.mapping
        expected = ScanResult(True, "APPROVED", str(self.identifier), 77, (91,))
        self.meals.record.return_value = expected
        self.assertIs(self.service.read(self.browser, self.identifier, self.token), expected)
        self.assertIsNone(self.meals.record.call_args.args[1].visitor_details)
        self.tx.execute.assert_not_called()

    def test_browser_cannot_use_another_browsers_request_for_read_or_result(self):
        self.tx.one.return_value = {**self.mapping, "browser_hash": b"a" * 32}
        for action in (
            lambda: self.service.read(self.browser, self.identifier, self.token),
            lambda: self.service.result(self.browser, self.identifier),
        ):
            self.assert_domain("REQUEST_MISMATCH", action)
        self.meals.record.assert_not_called()
        self.receipts.result.assert_not_called()
        self.tx.execute.assert_not_called()

    def test_result_recovery_requires_browser_ownership_before_shared_actor_lookup(self):
        self.tx.one.return_value = self.mapping
        expected = {"approved": True, "code": "APPROVED", "request_id": str(self.identifier), "meal_ids": [91], "serving_id": 77, "duplicate": True}
        self.receipts.result.return_value = expected
        self.assertIs(self.service.result(self.browser, self.identifier), expected)
        self.receipts.result.assert_called_once_with(ScanAppContext(42), self.identifier)
        self.tx.execute.assert_not_called()

    def test_unknown_result_has_no_fallback_to_arbitrary_staff_receipt(self):
        self.tx.one.return_value = None
        self.assert_domain("SCAN_RECEIPT_NOT_FOUND", lambda: self.service.result(self.browser, self.identifier))
        self.receipts.result.assert_not_called()
        self.tx.execute.assert_not_called()

    def test_settings_changes_do_not_retarget_an_existing_serving(self):
        self.tx.one.return_value = {**self.mapping, "scanner_code": "ORIGINAL-SCANNER", "meal_type_id": 8}
        self.service.read(self.browser, self.identifier, self.token)
        first = self.meals.record.call_args
        self.service.read(self.browser, self.identifier, self.token)
        for call in (first, self.meals.record.call_args):
            self.assertEqual(call.args[1].scanner_code, "ORIGINAL-SCANNER")
            self.assertEqual(call.args[1].meal_type_id, 8)
            self.assertEqual(call.args[0].staff_id, 42)
        self.assertTrue(all("FROM scan_app_requests" in call.args[0] for call in self.tx.one.call_args_list))
        self.tx.execute.assert_not_called()

    def test_invalid_browser_hash_or_request_uuid_fails_before_database_access(self):
        for browser in (None, "b" * 32, b"short", b"b" * 33):
            self.assert_domain("INVALID_SCANNER_BROWSER", lambda: self.service.read(browser, self.identifier, self.token))
        self.assert_domain("INVALID_REQUEST_ID", lambda: self.service.read(self.browser, "employee-1", self.token))
        self.db.transaction.assert_not_called()

    def test_unconfigured_or_inactive_default_profile_does_not_reserve_or_process(self):
        profiles = [None] + [{**self.profile, key: False} for key in (
            "is_enabled", "staff_active", "is_scanner", "scanner_active", "location_active", "meal_type_active",
        )]
        for profile in profiles:
            with self.subTest(profile=profile):
                self.tx.one.side_effect = [None, profile]
                self.assert_domain("SCANNER_NOT_CONFIGURED", lambda: self.service.read(self.browser, self.identifier, self.token))
        self.tx.execute.assert_not_called()
        self.meals.record.assert_not_called()

    def test_missing_migration_uses_clear_error_without_database_details(self):
        for number in (1054, 1146):
            error = RuntimeError("database details must remain private")
            error.errno = number
            self.tx.one.side_effect = error
            self.assert_domain("SCANNER_NOT_CONFIGURED", lambda: self.service.read(self.browser, self.identifier, self.token))
        self.meals.record.assert_not_called()

    def test_mapping_commit_failure_prevents_meal_processing(self):
        self.tx.one.side_effect = [None, self.profile]
        self.db.transaction.return_value.__exit__.side_effect = RuntimeError("Intentional mapping commit failure")
        with self.assertRaisesRegex(RuntimeError, "Intentional mapping commit failure"):
            self.service.read(self.browser, self.identifier, self.token)
        self.meals.record.assert_not_called()

    def test_mapping_read_commit_failure_prevents_result_release(self):
        self.tx.one.return_value = self.mapping
        self.db.transaction.return_value.__exit__.side_effect = RuntimeError("Intentional mapping commit failure")
        with self.assertRaisesRegex(RuntimeError, "Intentional mapping commit failure"):
            self.service.result(self.browser, self.identifier)
        self.receipts.result.assert_not_called()

    def test_same_browser_insert_race_reuses_committed_mapping(self):
        duplicate = RuntimeError("Intentional unique key race")
        duplicate.errno = 1062
        self.tx.one.side_effect = [None, self.profile, self.mapping]
        self.tx.execute.side_effect = duplicate
        self.service.read(self.browser, self.identifier, self.token)
        self.meals.record.assert_called_once()
        self.assertEqual(self.meals.record.call_args.args[1].request_id, self.identifier)
        self.assertEqual(self.tx.execute.call_count, 1)

    def test_different_browser_insert_race_cannot_claim_winning_mapping(self):
        duplicate = RuntimeError("Intentional unique key race")
        duplicate.errno = 1062
        self.tx.one.side_effect = [None, self.profile, {**self.mapping, "browser_hash": b"x" * 32}]
        self.tx.execute.side_effect = duplicate
        self.assert_domain("REQUEST_MISMATCH", lambda: self.service.read(self.browser, self.identifier, self.token))
        self.meals.record.assert_not_called()

    def test_invalid_input_audit_uses_configured_system_actor_and_scanner(self):
        self.tx.one.return_value = self.profile
        self.meals.record_invalid.return_value = ScanResult(False, "INVALID_INPUT", None)
        self.assertEqual(self.service.record_invalid(self.browser).code, "INVALID_INPUT")
        self.meals.record_invalid.assert_called_once_with(ScanAppContext(42), "SHARED-SCANNER")
        self.tx.execute.assert_not_called()

    def test_core_transaction_failure_cannot_be_converted_to_approval(self):
        self.tx.one.return_value = self.mapping
        self.meals.record.return_value = ScanResult(False, "PROCESSING_UNCONFIRMED", str(self.identifier))
        result = self.service.read(self.browser, self.identifier, self.token)
        self.assertFalse(result.approved)
        self.assertEqual(result.code, "PROCESSING_UNCONFIRMED")

    def test_latest_request_returns_only_canonical_uuid_and_quantity(self):
        self.tx.one.return_value = self.mapping
        result = self.service.latest_request(self.browser)
        self.assertEqual(result, {"request": {"request_id": str(self.identifier), "quantity": 1}})
        sql, parameters = self.tx.one.call_args.args
        self.assertEqual(parameters, (self.browser,))
        self.assertIn("WHERE browser_hash = %s", sql)
        self.assertIn("ORDER BY created_at DESC, request_id DESC LIMIT 1", sql)
        self.assertNotIn("FOR UPDATE", sql)
        self.tx.execute.assert_not_called()
        self.meals.record.assert_not_called()
        self.receipts.result.assert_not_called()

    def test_latest_request_is_empty_for_a_browser_without_owned_history(self):
        self.tx.one.side_effect = lambda sql, parameters: self.mapping if parameters == (self.browser,) else None
        self.assertEqual(self.service.latest_request(b"x" * 32), {"request": None})
        self.assertEqual(self.service.latest_request(self.browser)["request"]["request_id"], str(self.identifier))
        self.tx.execute.assert_not_called()

    def test_latest_request_requires_valid_browser_identity_before_database_access(self):
        for browser in (None, "b" * 32, b"short", b"b" * 33):
            self.assert_domain("INVALID_SCANNER_BROWSER", lambda: self.service.latest_request(browser))
        self.db.transaction.assert_not_called()

    def test_latest_request_preserves_a_mapping_without_inventing_a_final_result(self):
        self.tx.one.return_value = {"request_id": self.identifier.bytes}
        result = self.service.latest_request(self.browser)
        self.assertEqual(set(result["request"]), {"request_id", "quantity"})
        self.assertNotIn("approved", result)
        self.receipts.result.assert_not_called()
        self.tx.execute.assert_not_called()

    def test_latest_request_database_failure_does_not_claim_empty_history(self):
        self.tx.one.side_effect = RuntimeError("Intentional recovery query failure")
        with self.assertRaisesRegex(RuntimeError, "Intentional recovery query failure"):
            self.service.latest_request(self.browser)
        self.tx.execute.assert_not_called()

    def test_latest_request_missing_schema_uses_clear_setup_error(self):
        error = RuntimeError("Private missing table details")
        error.errno = 1146
        self.tx.one.side_effect = error
        self.assert_domain("SCANNER_NOT_CONFIGURED", lambda: self.service.latest_request(self.browser))

    def test_latest_request_transaction_failure_prevents_marker_release(self):
        self.tx.one.return_value = self.mapping
        self.db.transaction.return_value.__exit__.side_effect = RuntimeError("Intentional recovery transaction failure")
        with self.assertRaisesRegex(RuntimeError, "Intentional recovery transaction failure"):
            self.service.latest_request(self.browser)
