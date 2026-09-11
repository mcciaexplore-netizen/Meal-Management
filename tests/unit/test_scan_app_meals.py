import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import MagicMock, Mock, patch
from uuid import uuid4

from meal_management.errors import DomainError
from meal_management.meals import MealService, normalize_visitor_details, scan_fingerprint
from meal_management.models import Actor, ScanInput, ScanResult, ServerContext
from meal_management.security import generate_token, payload_digest, token_digest
from test_meals import FailingCommitDatabase


class VisitorDetailsTests(unittest.TestCase):
    def setUp(self):
        self.details = {"company_name": " Example Ltd ", "name": " Ravi Patel ", "email": " Ravi@Example.TEST ", "phone": " +91 (987) 654-3210 "}

    def test_details_are_canonical_without_changing_legacy_base_fingerprint(self):
        normalized = normalize_visitor_details(self.details)
        self.assertEqual(normalized, {
            "company_name": "Example Ltd", "name": "Ravi Patel", "email": "ravi@example.test", "phone": "+91 (987) 654-3210",
        })
        self.assertEqual(payload_digest(normalized), payload_digest(normalize_visitor_details(normalized)))
        original = scan_fingerprint(b"x" * 32, 42, 3, 4, 1, 1)
        self.assertEqual(original, payload_digest({
            "token_hash": (b"x" * 32).hex(), "staff_id": 42, "scanner_id": 3,
            "location_id": 4, "meal_type_id": 1, "quantity": 1,
        }))

    def test_missing_extra_blank_malformed_and_oversized_details_are_rejected(self):
        invalid = [None, {}, {**self.details, "role": "ADMIN"}, {**self.details, "name": " "},
                   {**self.details, "company_name": "x" * 151}, {**self.details, "email": "not-an-email"},
                   {**self.details, "phone": "123456"}, {**self.details, "phone": "+1234567890123456"},
                   {**self.details, "phone": "+91;SELECT 1"}, {**self.details, "phone": "９８７６５４３２１０"}]
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(DomainError):
                    normalize_visitor_details(value)

    def test_scan_representation_does_not_include_credentials_or_visitor_details(self):
        scan = ScanInput(uuid4(), generate_token(), 1, "SCANNER", visitor_details=self.details)
        self.assertNotIn(scan.token, repr(scan))
        self.assertNotIn("Ravi", repr(scan))
        self.assertNotIn("987", repr(scan))


class ScanAppMealTests(unittest.TestCase):
    def setUp(self):
        self.db = MagicMock()
        self.tx = self.db.transaction.return_value.__enter__.return_value
        self.tx.now.return_value = datetime(2026, 9, 9, 12)
        self.tx.execute.return_value = 1
        self.tx.insert.side_effect = [77, 91]
        self.service = MealService(self.db)
        self.context = ServerContext(generate_token())
        self.actor = Actor(42, frozenset({"WAITER"}))
        self.scan = ScanInput(uuid4(), generate_token(), 1, "SCANNER")
        self.details = {"company_name": "Example Ltd", "name": "Ravi Patel", "email": "ravi@example.test", "phone": "+919876543210"}
        self.fingerprint = scan_fingerprint(token_digest(self.scan.token), 42, 3, 4, 1, 1)
        self.receipt = {
            "request_id": self.scan.request_id.bytes, "request_text": str(self.scan.request_id), "attempt_id": 9,
            "digest": token_digest(self.scan.token), "fingerprint": self.fingerprint, "scanner_id": 3,
            "location_id": 4, "staff_id": 42, "visitor_details": None, "visitor_hash": None, "error": None,
        }
        self.scanner = {"id": 3, "location_id": 4, "is_active": True, "location_active": True}
        self.qr = {"id": 12, "kind": "MASTER", "employee_id": None, "revoked_at": None, "expires_at": None}
        self.auth = patch("meal_management.meals.require_scan_actor", return_value=self.actor)
        self.auth.start()
        self.addCleanup(self.auth.stop)

    def pending_rows(self, *, employee=False):
        qr = {**self.qr, "kind": "EMPLOYEE", "employee_id": 21} if employee else self.qr
        rows = [
            {"status": "PENDING", "payload_hash": self.fingerprint, "scan_bound_at": None},
            {"outcome": "RECEIVED"}, None, self.scanner, {"id": 1, "is_active": True}, qr,
        ]
        if employee:
            rows.append({"id": 21, "is_active": True})
        rows.append(qr)
        return rows

    def test_master_read_validates_then_waits_without_meal_or_proof_binding(self):
        self.receipt["reading"] = True
        self.tx.one.side_effect = self.pending_rows() + [None]
        result = self.service._process(self.context, self.scan, self.receipt)
        self.assertEqual(result, {"kind": "MASTER", "next": "VISITOR_DETAILS", "request_id": str(self.scan.request_id)})
        self.tx.insert.assert_not_called()
        writes = [call.args[0] for call in self.tx.execute.call_args_list]
        self.assertTrue(any("outcome = 'AWAITING_DETAILS'" in sql for sql in writes))
        self.assertFalse(any(sql.startswith("UPDATE serving_requests") for sql in writes))

    def test_employee_read_records_one_meal_and_binds_no_visitor_details(self):
        self.receipt["reading"] = True
        self.tx.one.side_effect = self.pending_rows(employee=True)
        result = self.service._process(self.context, self.scan, self.receipt)
        self.assertTrue(result.approved)
        self.assertEqual(result.meal_ids, (91,))
        self.assertEqual(self.tx.insert.call_args_list[1].args[1][1:3], (1, 1))
        binding = next(call for call in self.tx.execute.call_args_list if "SET scan_authorization_id" in call.args[0])
        self.assertEqual(binding.args[1][:2], (None, None))

    def test_direct_master_records_one_meal_with_four_details_and_no_authorization(self):
        scan = replace(self.scan, visitor_details=self.details)
        self.receipt.update(visitor_details=self.details, visitor_hash=payload_digest(self.details))
        self.tx.one.side_effect = self.pending_rows()
        result = self.service._process(self.context, scan, self.receipt)
        self.assertTrue(result.approved)
        self.assertEqual(result.meal_ids, (91,))
        sql, values = self.tx.insert.call_args_list[0].args
        self.assertIn("visitor_company_name, visitor_name, visitor_email, visitor_phone", sql)
        self.assertEqual(values[9], None)
        self.assertEqual(values[-4:], tuple(self.details.values()))
        binding = next(call for call in self.tx.execute.call_args_list if "SET scan_authorization_id" in call.args[0])
        self.assertEqual(binding.args[1][:2], (None, payload_digest(self.details)))
        self.assertFalse(any("FROM visitor_authorizations WHERE id" in call.args[0] for call in self.tx.one.call_args_list))

    def test_direct_master_without_details_rejects_attempt_and_leaves_request_unbound(self):
        self.tx.one.side_effect = self.pending_rows() + [None, {"id": 12}]
        result = self.service._process(self.context, self.scan, self.receipt)
        self.assertFalse(result.approved)
        self.assertEqual(result.code, "VISITOR_DETAILS_REQUIRED")
        self.tx.insert.assert_not_called()
        self.assertFalse(any(call.args[0].startswith("UPDATE serving_requests") for call in self.tx.execute.call_args_list))

    def test_employee_cannot_receive_visitor_details(self):
        scan = replace(self.scan, visitor_details=self.details)
        self.receipt.update(visitor_details=self.details, visitor_hash=payload_digest(self.details))
        self.tx.one.side_effect = self.pending_rows(employee=True) + [{"id": 12}]
        result = self.service._process(self.context, scan, self.receipt)
        self.assertEqual(result.code, "VISITOR_DETAILS_NOT_APPLICABLE")
        self.tx.insert.assert_not_called()

    def terminal(self, visitor_hash, reading=False):
        self.receipt.update(reading=reading)
        self.tx.one.side_effect = [
            {"status": "SUCCEEDED", "payload_hash": self.fingerprint, "scan_bound_at": self.tx.now(),
             "scan_authorization_id": None, "scan_visitor_hash": visitor_hash},
            {"outcome": "RECEIVED"}, {"id": 77, "qr_id": 12, "authorization_id": None},
        ]
        self.tx.all.return_value = [{"id": 91}]

    def test_direct_master_final_retry_preserves_original_and_rejects_changed_details(self):
        scan = replace(self.scan, visitor_details=self.details)
        digest = payload_digest(self.details)
        self.receipt.update(visitor_details=self.details, visitor_hash=digest)
        self.terminal(digest)
        result = self.service._process(self.context, scan, self.receipt)
        self.assertTrue(result.approved)
        self.assertTrue(result.duplicate)
        self.assertEqual(result.serving_id, 77)
        self.assertEqual(result.meal_ids, (91,))
        self.terminal(payload_digest({**self.details, "name": "Different Visitor"}))
        result = self.service._process(self.context, scan, self.receipt)
        self.assertFalse(result.approved)
        self.assertEqual(result.code, "IDEMPOTENCY_KEY_REUSED")
        self.tx.insert.assert_not_called()

    def test_master_read_replays_original_commit_without_resubmitting_private_details(self):
        self.terminal(payload_digest(self.details), reading=True)
        result = self.service._process(self.context, self.scan, self.receipt)
        self.assertTrue(result.approved)
        self.assertTrue(result.duplicate)
        self.assertEqual(result.meal_ids, (91,))
        self.tx.insert.assert_not_called()

    def test_legacy_commit_cannot_acquire_new_visitor_details_on_replay(self):
        self.receipt.update(visitor_details=self.details, visitor_hash=payload_digest(self.details))
        self.tx.one.side_effect = [
            {"status": "SUCCEEDED", "payload_hash": self.fingerprint}, {"outcome": "RECEIVED"},
        ]
        result = self.service._process(self.context, replace(self.scan, visitor_details=self.details), self.receipt)
        self.assertEqual(result.code, "IDEMPOTENCY_KEY_REUSED")
        self.tx.all.assert_not_called()

    def test_master_read_never_bypasses_base_caller_binding(self):
        self.receipt["reading"] = True
        self.tx.one.side_effect = [
            {"status": "PENDING", "payload_hash": b"x" * 32}, {"outcome": "RECEIVED"},
        ]
        result = self.service._process(self.context, self.scan, self.receipt)
        self.assertEqual(result.code, "IDEMPOTENCY_KEY_REUSED")
        self.tx.insert.assert_not_called()

    def test_invalid_master_read_rejection_binds_and_finishes_without_details(self):
        self.qr["expires_at"] = self.tx.now() - timedelta(seconds=1)
        self.receipt["reading"] = True
        self.tx.one.side_effect = self.pending_rows() + [{"id": 12}]
        result = self.service._process(self.context, self.scan, self.receipt)
        self.assertEqual(result.code, "QR_EXPIRED")
        self.tx.insert.assert_not_called()
        self.assertTrue(any("status = 'REJECTED'" in call.args[0] for call in self.tx.execute.call_args_list))

    def test_waiting_response_is_not_returned_before_commit(self):
        self.receipt["reading"] = True
        self.tx.one.side_effect = self.pending_rows() + [None]
        service = MealService(FailingCommitDatabase(self.tx))
        with patch.object(service, "_receive", return_value=self.receipt), patch.object(service, "_mark_interrupted"):
            result = service.read(self.context, self.scan)
        self.assertIsInstance(result, ScanResult)
        self.assertFalse(result.approved)
        self.assertEqual(result.code, "PROCESSING_UNCONFIRMED")

    def test_direct_master_approval_is_not_returned_when_commit_fails(self):
        self.receipt.update(visitor_details=self.details, visitor_hash=payload_digest(self.details))
        self.tx.one.side_effect = self.pending_rows()
        service = MealService(FailingCommitDatabase(self.tx))
        with patch.object(service, "_receive", return_value=self.receipt), patch.object(service, "_mark_interrupted"):
            result = service.record(self.context, replace(self.scan, visitor_details=self.details))
        self.assertFalse(result.approved)
        self.assertIsNone(result.serving_id)
        self.assertEqual(result.code, "PROCESSING_UNCONFIRMED")

    def test_allocated_master_records_one_meal_and_exhausts_on_final_allowance(self):
        allocation = {
            "qr_id": 12, "company_name": "Example Ltd", "contact_name": "Ravi Patel",
            "email": "ravi@example.test", "phone": "+919876543210", "meal_limit": 5,
            "meals_used": 4, "exhausted_at": None,
        }
        self.receipt["public_scanner"] = True
        self.tx.one.side_effect = self.pending_rows() + [allocation]
        result = self.service._process(self.context, self.scan, self.receipt)
        self.assertTrue(result.approved)
        self.assertEqual(result.meal_ids, (91,))
        serving = self.tx.insert.call_args_list[0].args[1]
        self.assertEqual(serving[-4:], ("Example Ltd", "Ravi Patel", "ravi@example.test", "+919876543210"))
        allowance = next(call for call in self.tx.execute.call_args_list if call.args[0].startswith("UPDATE master_qr_allocations"))
        self.assertIn("meals_used < meal_limit", allowance.args[0])
        self.assertEqual(allowance.args[1], (self.tx.now(), 12))

    def test_exhausted_allocated_master_is_rejected_without_a_meal(self):
        allocation = {
            "qr_id": 12, "company_name": "Example Ltd", "contact_name": "Ravi Patel",
            "email": "ravi@example.test", "phone": "+919876543210", "meal_limit": 5,
            "meals_used": 5, "exhausted_at": self.tx.now(),
        }
        self.receipt["public_scanner"] = True
        self.tx.one.side_effect = self.pending_rows() + [allocation, {"id": 12}]
        result = self.service._process(self.context, self.scan, self.receipt)
        self.assertFalse(result.approved)
        self.assertEqual(result.code, "QR_EXPIRED")
        self.tx.insert.assert_not_called()

    def test_invalid_details_are_logged_without_reserving_or_poisoning_request(self):
        self.tx.one.side_effect = [{"id": 3, "location_id": 4}, None]
        self.tx.insert.side_effect = None
        self.tx.insert.return_value = 9
        receipt = self.service._receive(self.context, replace(self.scan, visitor_details={"name": "Ravi"}))
        self.assertEqual(receipt["error"], "INVALID_VISITOR_DETAILS")
        self.assertIsNone(receipt["request_id"])
        self.assertFalse(any("INSERT INTO serving_requests" in call.args[0] for call in self.tx.execute.call_args_list))
        self.assertEqual(self.tx.insert.call_args.args[1][7], "REJECTED")

    def test_read_rejects_quantity_authorization_or_supplied_details(self):
        self.tx.insert.side_effect = None
        self.tx.insert.return_value = 9
        for scan in (
            replace(self.scan, quantity=2), replace(self.scan, authorization_id=7),
            replace(self.scan, visitor_details=self.details),
        ):
            with self.subTest(scan=scan):
                self.tx.one.side_effect = [{"id": 3, "location_id": 4}, None]
                receipt = self.service._receive(self.context, scan, reading=True)
                self.assertEqual(receipt["error"], "INVALID_SCAN_READ_INPUT")
                self.assertIsNone(receipt["request_id"])

    def test_direct_details_reject_quantity_or_legacy_authorization_before_binding(self):
        self.tx.insert.side_effect = None
        self.tx.insert.return_value = 9
        for scan, code in (
            (replace(self.scan, visitor_details=self.details, quantity=2), "VISITOR_QUANTITY_MUST_BE_ONE"),
            (replace(self.scan, visitor_details=self.details, authorization_id=7), "VISITOR_DETAILS_AUTHORIZATION_CONFLICT"),
        ):
            with self.subTest(code=code):
                self.tx.one.side_effect = [{"id": 3, "location_id": 4}, None]
                receipt = self.service._receive(self.context, scan)
                self.assertEqual(receipt["error"], code)
                self.assertIsNone(receipt["request_id"])

    def test_malformed_token_on_existing_request_cannot_claim_original_final_rejection(self):
        self.tx.one.side_effect = [{"id": 3, "location_id": 4}, {"id": self.scan.request_id.bytes}]
        self.tx.insert.side_effect = None
        self.tx.insert.return_value = 9
        result = self.service.read(self.context, replace(self.scan, token="https://not-a-meal-qr.example.test"))
        self.assertEqual(result.code, "REQUEST_MISMATCH")
        self.assertFalse(result.approved)
        self.assertEqual(result.request_id, str(self.scan.request_id))
        self.assertEqual(self.tx.one.call_args.args[1], (self.scan.request_id.bytes,))
        logged = self.tx.insert.call_args.args[1]
        self.assertIsNone(logged[0])
        self.assertIsNone(logged[1])
        self.assertIsNone(logged[2])
        self.assertEqual(logged[7:9], ("REJECTED", "INVALID_QR"))
        self.tx.execute.assert_not_called()

    def test_every_prebinding_validation_error_preserves_existing_request(self):
        cases = [
            (replace(self.scan, quantity=0), "INVALID_QUANTITY"),
            (replace(self.scan, meal_type_id=0), "INVALID_MEAL_TYPE_ID"),
            (replace(self.scan, scanner_code=" "), "INVALID_SCANNER_CODE"),
            (replace(self.scan, authorization_id=0), "INVALID_AUTHORIZATION_ID"),
            (replace(self.scan, visitor_details={"name": "Only Name"}), "INVALID_VISITOR_DETAILS"),
        ]
        self.tx.insert.side_effect = None
        self.tx.insert.return_value = 9
        for scan, reason in cases:
            with self.subTest(reason=reason):
                self.tx.one.side_effect = [{"id": 3, "location_id": 4}, {"id": self.scan.request_id.bytes}]
                result = self.service.record(self.context, scan)
                self.assertEqual(result.code, "REQUEST_MISMATCH")
                logged = self.tx.insert.call_args.args[1]
                self.assertIsNone(logged[0])
                self.assertIsNone(logged[1])
                self.assertEqual(logged[8], reason)
        self.tx.execute.assert_not_called()

    def test_malformed_token_for_fresh_request_keeps_its_actual_rejection(self):
        self.tx.one.side_effect = [{"id": 3, "location_id": 4}, None]
        self.tx.insert.side_effect = None
        self.tx.insert.return_value = 9
        result = self.service.read(self.context, replace(self.scan, token="not-a-credential"))
        self.assertEqual(result.code, "INVALID_QR")
        self.assertFalse(result.approved)
        self.tx.execute.assert_not_called()
