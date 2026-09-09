import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, Mock, patch
from uuid import uuid4

from meal_management.errors import DomainError
from meal_management.meals import ApprovalService, MealService, scan_fingerprint
from meal_management.models import Actor, ScanInput, ServerContext
from meal_management.scan_receipts import ScanReceiptService
from meal_management.security import generate_token, token_digest


class ReceiptCommitDatabase:
    def __init__(self, transaction):
        self.transaction_session = transaction

    @contextmanager
    def transaction(self):
        yield self.transaction_session
        raise RuntimeError("Intentional receipt transaction failure")


class ScanReceiptTests(unittest.TestCase):
    def setUp(self):
        self.db = MagicMock()
        self.tx = self.db.transaction.return_value.__enter__.return_value
        self.service = ScanReceiptService(self.db)
        self.context = ServerContext(generate_token())
        self.identifier = uuid4()
        self.actor = Actor(42, frozenset({"WAITER"}))
        self.now = datetime(2026, 9, 9, 12)
        self.row = {
            "serving_id": 77, "waiter_id": 42, "kind": "EMPLOYEE", "quantity": 1,
            "served_at": self.now, "employee_id": 21, "employee_name": "Asha Rao",
            "selfie_object_key": "a" * 64 + ".jpg", "visitor_name": None, "meal_count": 1,
        }
        self.auth = patch("meal_management.scan_receipts.require_scan_actor", return_value=self.actor)
        self.require_actor = self.auth.start()
        self.addCleanup(self.auth.stop)

    def approved(self):
        self.tx.one.side_effect = [{"status": "SUCCEEDED"}, dict(self.row)]

    def test_approved_employee_receipt_has_only_scoped_public_fields(self):
        self.approved()
        receipt = self.service.get(self.context, str(self.identifier).upper())
        self.assertEqual(receipt, {
            "request_id": str(self.identifier), "serving_id": 77, "kind": "EMPLOYEE",
            "quantity": 1, "served_at": self.now.replace(tzinfo=timezone.utc),
            "employee_name": "Asha Rao", "employee_id": 21, "photo_available": True,
            "visitor_name": None,
        })
        self.require_actor.assert_called_once_with(self.tx, self.context)
        self.assertEqual(self.tx.one.call_args_list[0].args[1], (self.identifier.bytes, 42, 42, 42))
        self.assertEqual(self.tx.one.call_args_list[1].args[1], (self.identifier.bytes, 42))
        self.assertNotIn("token", " ".join(call.args[0] for call in self.tx.one.call_args_list))

    def test_admin_can_read_only_own_receipt(self):
        self.require_actor.return_value = Actor(42, frozenset({"ADMIN"}))
        self.approved()
        receipt = self.service.get(self.context, self.identifier)
        self.assertEqual(receipt["serving_id"], 77)
        self.assertIn("s.waiter_id = %s", self.tx.one.call_args.args[0])

    def test_other_admin_cannot_read_arbitrary_receipt_or_photo(self):
        self.require_actor.return_value = Actor(7, frozenset({"ADMIN"}))
        for method in (self.service.get, self.service.photo_key):
            with self.subTest(method=method):
                self.tx.one.reset_mock()
                self.tx.one.side_effect = None
                self.tx.one.return_value = None
                with self.assertRaises(DomainError) as caught:
                    method(self.context, self.identifier)
                self.assertEqual(caught.exception.code, "SCAN_RECEIPT_NOT_FOUND")
                self.tx.one.assert_called_once()
                self.assertNotIn("employee_name", self.tx.one.call_args.args[0])

    def test_returned_serving_principal_is_checked_again(self):
        self.row["waiter_id"] = 99
        self.approved()
        with self.assertRaises(DomainError) as caught:
            self.service.get(self.context, self.identifier)
        self.assertEqual(caught.exception.code, "SCAN_RECEIPT_NOT_FOUND")

    def test_pending_and_rejected_requests_do_not_query_names_or_photos(self):
        for status, code in (("PENDING", "SCAN_PENDING"), ("REJECTED", "SCAN_REJECTED")):
            for method in (self.service.get, self.service.photo_key):
                with self.subTest(status=status, method=method):
                    self.tx.one.reset_mock()
                    self.tx.one.side_effect = [{"status": status}]
                    with self.assertRaises(DomainError) as caught:
                        method(self.context, self.identifier)
                    self.assertEqual(caught.exception.code, code)
                    self.tx.one.assert_called_once()
                    self.assertNotIn("selfie_object_key", self.tx.one.call_args.args[0])
                    self.assertNotIn("JOIN employees", self.tx.one.call_args.args[0])

    def test_unverified_request_uuid_does_not_allow_employee_lookup(self):
        self.tx.one.return_value = None
        with self.assertRaises(DomainError) as caught:
            self.service.get(self.context, uuid4())
        self.assertEqual(caught.exception.code, "SCAN_RECEIPT_NOT_FOUND")
        self.tx.one.assert_called_once()

    def test_receipt_uses_original_committed_serving_on_repeated_lookup(self):
        self.tx.one.side_effect = [{"status": "SUCCEEDED"}, dict(self.row), {"status": "SUCCEEDED"}, dict(self.row)]
        first = self.service.get(self.context, self.identifier)
        second = self.service.get(self.context, self.identifier)
        self.assertEqual(second, first)
        self.tx.insert.assert_not_called()
        self.tx.execute.assert_not_called()

    def test_partial_batch_cannot_be_displayed_as_approved(self):
        self.row.update(kind="MASTER", quantity=3, meal_count=2, visitor_name="Ravi Patel")
        self.approved()
        with self.assertRaises(DomainError) as caught:
            self.service.get(self.context, self.identifier)
        self.assertEqual(caught.exception.code, "PROCESSING_UNCONFIRMED")

    def test_master_receipt_does_not_expose_employee_fields_or_photo(self):
        self.row.update(kind="MASTER", quantity=3, meal_count=3, visitor_name="Ravi Patel")
        self.approved()
        receipt = self.service.get(self.context, self.identifier)
        self.assertEqual(receipt["visitor_name"], "Ravi Patel")
        self.assertEqual(receipt["quantity"], 3)
        self.assertIsNone(receipt["employee_name"])
        self.assertIsNone(receipt["employee_id"])
        self.assertFalse(receipt["photo_available"])
        self.approved()
        with self.assertRaises(DomainError) as caught:
            self.service.photo_key(self.context, self.identifier)
        self.assertEqual(caught.exception.code, "PHOTO_NOT_FOUND")

    def test_missing_master_authorization_details_fail_closed(self):
        self.row.update(kind="MASTER", visitor_name=None)
        self.approved()
        with self.assertRaises(DomainError) as caught:
            self.service.get(self.context, self.identifier)
        self.assertEqual(caught.exception.code, "PROCESSING_UNCONFIRMED")

    def test_photo_key_requires_same_approved_request_scope(self):
        self.approved()
        key = self.service.photo_key(self.context, self.identifier)
        self.assertEqual(key, self.row["selfie_object_key"])
        self.assertEqual(self.tx.one.call_args.args[1], (self.identifier.bytes, 42))

    def test_employee_without_photo_returns_available_false_and_no_photo(self):
        self.row["selfie_object_key"] = None
        self.approved()
        self.assertFalse(self.service.get(self.context, self.identifier)["photo_available"])
        self.approved()
        with self.assertRaises(DomainError) as caught:
            self.service.photo_key(self.context, self.identifier)
        self.assertEqual(caught.exception.code, "PHOTO_NOT_FOUND")

    def test_authentication_failure_stops_all_receipt_queries(self):
        self.require_actor.side_effect = DomainError("AUTHENTICATION_REQUIRED")
        with self.assertRaises(DomainError):
            self.service.get(self.context, self.identifier)
        self.tx.one.assert_not_called()

    def test_non_scanning_role_is_denied_before_receipt_queries(self):
        self.require_actor.side_effect = DomainError("ROLE_REQUIRED")
        with self.assertRaises(DomainError):
            self.service.photo_key(self.context, self.identifier)
        self.tx.one.assert_not_called()

    def test_arbitrary_employee_identifier_is_not_a_receipt_identifier(self):
        with self.assertRaises(DomainError) as caught:
            self.service.photo_key(self.context, 21)
        self.assertEqual(caught.exception.code, "INVALID_REQUEST_ID")
        self.db.transaction.assert_not_called()

    def test_receipt_is_not_returned_when_read_transaction_commit_fails(self):
        self.approved()
        service = ScanReceiptService(ReceiptCommitDatabase(self.tx))
        with self.assertRaisesRegex(RuntimeError, "Intentional receipt transaction failure"):
            service.get(self.context, self.identifier)

    def result_approved(self, quantity=2, kind="MASTER"):
        self.tx.one.side_effect = [
            {"status": "SUCCEEDED", "rejection_code": None},
            {"id": 77, "waiter_id": 42, "quantity": quantity, "kind": kind},
        ]
        self.tx.all.return_value = [
            {"id": 90 + unit, "unit_number": unit, "serving_quantity": quantity}
            for unit in range(1, quantity + 1)
        ]

    def test_result_recovers_original_committed_meal_ids_without_qr_or_personal_details(self):
        for kind, quantity in (("EMPLOYEE", 1), ("MASTER", 3)):
            with self.subTest(kind=kind):
                self.result_approved(quantity, kind)
                result = self.service.result(self.context, self.identifier)
                self.assertEqual(result, {
                    "approved": True, "code": "APPROVED", "request_id": str(self.identifier),
                    "serving_id": 77, "meal_ids": list(range(91, 91 + quantity)), "duplicate": True,
                })
        statements = " ".join(call.args[0] for call in self.tx.one.call_args_list)
        self.assertNotIn("JOIN employees", statements)
        self.assertNotIn("token", statements)
        self.assertNotIn("visitor_name", statements)
        self.tx.insert.assert_not_called()
        self.tx.execute.assert_not_called()

    def test_result_returns_original_rejection_without_loading_personal_details(self):
        self.tx.one.side_effect = [{"status": "REJECTED", "rejection_code": "QR_REVOKED"}]
        result = self.service.result(self.context, self.identifier)
        self.assertEqual(result, {
            "approved": False, "code": "QR_REVOKED", "request_id": str(self.identifier),
            "serving_id": None, "meal_ids": [], "duplicate": True,
        })
        self.tx.one.assert_called_once()
        self.tx.all.assert_not_called()
        self.tx.insert.assert_not_called()

    def test_pending_result_is_unconfirmed_without_processing_another_scan(self):
        self.tx.one.side_effect = [{"status": "PENDING", "rejection_code": None}]
        result = self.service.result(self.context, self.identifier)
        self.assertFalse(result["approved"])
        self.assertEqual(result["code"], "PROCESSING_UNCONFIRMED")
        self.assertEqual(result["meal_ids"], [])
        self.tx.one.assert_called_once()
        self.tx.all.assert_not_called()
        self.tx.execute.assert_not_called()

    def test_result_scope_does_not_expand_for_another_admin(self):
        self.require_actor.return_value = Actor(7, frozenset({"ADMIN"}))
        self.tx.one.return_value = None
        with self.assertRaises(DomainError) as caught:
            self.service.result(self.context, self.identifier)
        self.assertEqual(caught.exception.code, "SCAN_RECEIPT_NOT_FOUND")
        self.assertEqual(self.tx.one.call_args.args[1], (self.identifier.bytes, 7, 7, 7))
        self.tx.all.assert_not_called()

    def test_result_fails_closed_for_incomplete_or_inconsistent_units(self):
        invalid_batches = [
            [{"id": 91, "unit_number": 1, "serving_quantity": 2}],
            [{"id": 91, "unit_number": 1, "serving_quantity": 2},
             {"id": 92, "unit_number": 3, "serving_quantity": 2}],
            [{"id": 91, "unit_number": 1, "serving_quantity": 2},
             {"id": 92, "unit_number": 2, "serving_quantity": 3}],
            [{"id": 91, "unit_number": 1, "serving_quantity": 2},
             {"id": 91, "unit_number": 2, "serving_quantity": 2}],
        ]
        for meals in invalid_batches:
            with self.subTest(meals=meals):
                self.result_approved()
                self.tx.all.return_value = meals
                result = self.service.result(self.context, self.identifier)
                self.assertFalse(result["approved"])
                self.assertEqual(result["code"], "PROCESSING_UNCONFIRMED")
                self.assertIsNone(result["serving_id"])
                self.assertEqual(result["meal_ids"], [])

    def test_result_is_not_returned_before_successful_commit(self):
        self.result_approved()
        service = ScanReceiptService(ReceiptCommitDatabase(self.tx))
        with self.assertRaisesRegex(RuntimeError, "Intentional receipt transaction failure"):
            service.result(self.context, self.identifier)


class ServingStaffAccessTests(unittest.TestCase):
    def setUp(self):
        self.db = MagicMock()
        self.tx = self.db.transaction.return_value.__enter__.return_value
        self.tx.now.return_value = datetime(2026, 9, 9, 12)
        self.tx.execute.return_value = 1
        self.actor = Actor(7, frozenset({"ADMIN"}))
        self.context = ServerContext(generate_token())

    def test_invalid_admin_scan_is_recorded_with_authenticated_admin_identity(self):
        with patch("meal_management.meals.require_scan_actor", return_value=self.actor) as require:
            MealService(self.db).record_invalid(self.context)
        require.assert_called_once_with(self.tx, self.context)
        self.assertEqual(self.tx.insert.call_args.args[1][0], 7)

    def test_admin_scan_receipt_and_processing_allow_admin_role(self):
        service = MealService(self.db)
        scan = ScanInput(uuid4(), generate_token(), 1, "SCANNER")
        fingerprint = scan_fingerprint(token_digest(scan.token), 7, 3, 4, 1, 1)
        self.tx.one.side_effect = [{"id": 3, "location_id": 4}, {"status": "PENDING", "payload_hash": fingerprint}]
        with patch("meal_management.meals.require_scan_actor", return_value=self.actor) as require:
            receipt = service._receive(self.context, scan)
        require.assert_called_once_with(self.tx, self.context)
        self.assertEqual(receipt["staff_id"], 7)
        self.tx.one.side_effect = [
            {"status": "SUCCEEDED", "payload_hash": fingerprint}, {"outcome": "RECEIVED"},
            {"id": 77, "qr_id": 12, "authorization_id": None},
        ]
        self.tx.all.return_value = [{"id": 91}]
        with patch("meal_management.meals.require_scan_actor", return_value=self.actor) as require:
            result = service._process(self.context, scan, receipt)
        require.assert_called_once_with(self.tx, self.context)
        self.assertTrue(result.approved)
        self.assertTrue(result.duplicate)

    def test_admin_can_be_target_of_visitor_authorization(self):
        identifier = uuid4()
        token = generate_token()
        fingerprint = scan_fingerprint(token_digest(token), 7, 3, 4, 1, 2)
        self.tx.one.side_effect = [
            {"id": 7, "is_active": True}, {"role_code": "ADMIN"},
            {"id": 3, "location_id": 4, "is_active": True, "location_active": True},
            {"id": 1, "is_active": True}, {"id": 12, "kind": "MASTER", "revoked_at": None, "expires_at": None},
            {"status": "PENDING", "payload_hash": fingerprint}, None,
        ]
        self.tx.insert.return_value = 51
        with patch("meal_management.meals.require_actor", return_value=self.actor), patch("meal_management.meals.audit"):
            authorization = ApprovalService(self.db).authorize(
                self.context, identifier, token, 7, "SCANNER", 1, 2, "Ravi Patel"
            )
        self.assertEqual(authorization, 51)
        self.assertIn("role_code IN ('ADMIN', 'WAITER')", self.tx.one.call_args_list[1].args[0])
        self.assertEqual(self.tx.insert.call_args.args[1][5], 7)

    def test_waiter_cannot_gain_admin_authorization_permission(self):
        with patch("meal_management.meals.require_actor", side_effect=DomainError("ROLE_REQUIRED")) as require:
            with self.assertRaises(DomainError):
                ApprovalService(self.db).authorize(
                    self.context, uuid4(), generate_token(), 7, "SCANNER", 1, 1, "Visitor"
                )
        require.assert_called_once_with(self.tx, self.context, {"ADMIN"})
        self.tx.insert.assert_not_called()


if __name__ == "__main__":
    unittest.main()
