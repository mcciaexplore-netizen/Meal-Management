import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta
from uuid import uuid4
from unittest.mock import MagicMock, Mock, patch

from meal_management.errors import DomainError
from meal_management.meals import AttemptRejection, MealService, positive_integer, request_uuid, scan_fingerprint
from meal_management.models import Actor, ScanInput, ScanResult, ServerContext
from meal_management.security import generate_token, token_digest


class FailingCommitDatabase:
    def __init__(self, transaction):
        self.session = transaction
        self.commit_attempted = False

    @contextmanager
    def transaction(self):
        yield self.session
        self.commit_attempted = True
        raise RuntimeError("intentional commit failure")


class MealCommitTests(unittest.TestCase):
    def test_approval_is_not_returned_when_meal_commit_fails(self):
        identifier = uuid4()
        transaction = Mock()
        transaction.one.side_effect = [
            {"status": "PENDING", "payload_hash": b"f" * 32},
            {"outcome": "RECEIVED"},
            None,
        ]
        database = FailingCommitDatabase(transaction)
        service = MealService(database)
        receipt = {
            "attempt_id": 9,
            "request_id": identifier.bytes,
            "request_text": str(identifier),
            "fingerprint": b"f" * 32,
            "digest": b"d" * 32,
            "staff_id": 42,
            "error": None,
        }
        scan = ScanInput(identifier, generate_token(), 1, "SCANNER")
        intended_result = ScanResult(True, "APPROVED", str(identifier), 77, (91,))
        with (
            patch.object(service, "_receive", return_value=receipt),
            patch.object(service, "_create_serving", return_value=intended_result) as create,
            patch.object(service, "_mark_interrupted") as interrupted,
            patch("meal_management.meals.require_scan_actor", return_value=Actor(42, frozenset({"WAITER"}))),
        ):
            result = service.record(ServerContext(generate_token()), scan)
        self.assertFalse(result.approved)
        self.assertEqual(result.code, "PROCESSING_UNCONFIRMED")
        self.assertIsNone(result.serving_id)
        self.assertTrue(database.commit_attempted)
        create.assert_called_once()
        interrupted.assert_called_once_with(9)

    def test_unconfirmed_scan_receipt_prevents_meal_processing(self):
        service = MealService(Mock())
        scan = ScanInput(uuid4(), generate_token(), 1, "SCANNER")
        with (
            patch.object(service, "_receive", side_effect=RuntimeError("receipt commit failed")),
            patch.object(service, "_process") as process,
        ):
            with self.assertRaises(DomainError) as caught:
                service.record(ServerContext(generate_token()), scan)
        self.assertEqual(caught.exception.code, "SCAN_RECEIPT_UNCONFIRMED")
        process.assert_not_called()

    def test_final_rejection_does_not_start_a_meal_transaction(self):
        identifier = uuid4()
        service = MealService(Mock())
        scan = ScanInput(identifier, "malformed", 1, "SCANNER")
        with (
            patch.object(service, "_receive", return_value={"error": "INVALID_QR", "request_text": str(identifier)}),
            patch.object(service, "_process") as process,
        ):
            result = service.record(ServerContext(generate_token()), scan)
        self.assertFalse(result.approved)
        self.assertEqual(result.code, "INVALID_QR")
        process.assert_not_called()


class MealIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.identifier = uuid4()
        self.scan = ScanInput(self.identifier, generate_token(), 1, "SCANNER")
        self.context = ServerContext(generate_token())
        self.receipt = {
            "attempt_id": 9,
            "request_id": self.identifier.bytes,
            "request_text": str(self.identifier),
            "fingerprint": b"f" * 32,
            "staff_id": 42,
            "error": None,
        }
        self.database = MagicMock()
        self.transaction = self.database.transaction.return_value.__enter__.return_value
        self.transaction.execute.return_value = 1
        self.service = MealService(self.database)

    def test_exact_retry_returns_original_meal_without_another_insert(self):
        self.transaction.one.side_effect = [
            {"status": "SUCCEEDED", "payload_hash": b"f" * 32},
            {"outcome": "RECEIVED"},
            {"id": 77, "qr_id": 12, "authorization_id": None},
        ]
        self.transaction.all.return_value = [{"id": 91}]
        with (
            patch.object(self.service, "_receive", return_value=self.receipt),
            patch.object(self.service, "_create_serving") as create,
            patch("meal_management.meals.require_scan_actor", return_value=Actor(42, frozenset({"WAITER"}))),
        ):
            result = self.service.record(self.context, self.scan)
        self.assertTrue(result.approved)
        self.assertTrue(result.duplicate)
        self.assertEqual(result.code, "APPROVED")
        self.assertEqual(result.serving_id, 77)
        self.assertEqual(result.meal_ids, (91,))
        create.assert_not_called()
        self.transaction.insert.assert_not_called()

    def test_same_identifier_with_different_payload_is_rejected(self):
        self.transaction.one.side_effect = [
            {"status": "SUCCEEDED", "payload_hash": b"x" * 32},
            {"outcome": "RECEIVED"},
        ]
        with (
            patch.object(self.service, "_receive", return_value=self.receipt),
            patch.object(self.service, "_create_serving") as create,
            patch("meal_management.meals.require_scan_actor", return_value=Actor(42, frozenset({"WAITER"}))),
        ):
            result = self.service.record(self.context, self.scan)
        self.assertFalse(result.approved)
        self.assertEqual(result.code, "IDEMPOTENCY_KEY_REUSED")
        create.assert_not_called()
        self.transaction.insert.assert_not_called()

    def test_rejected_retry_returns_original_result_when_proof_matches(self):
        self.transaction.one.side_effect = [
            {"status": "REJECTED", "payload_hash": b"f" * 32, "rejection_code": "QR_EXPIRED",
             "scan_authorization_id": None, "scan_bound_at": datetime(2026, 9, 9)},
            {"outcome": "RECEIVED"},
        ]
        with (
            patch.object(self.service, "_receive", return_value=self.receipt),
            patch("meal_management.meals.require_scan_actor", return_value=Actor(42, frozenset({"WAITER"}))),
        ):
            result = self.service.record(self.context, self.scan)
        self.assertFalse(result.approved)
        self.assertTrue(result.duplicate)
        self.assertEqual(result.code, "QR_EXPIRED")
        self.transaction.insert.assert_not_called()

    def test_rejected_retry_with_changed_authorization_is_rejected_as_key_reuse(self):
        self.transaction.one.side_effect = [
            {"status": "REJECTED", "payload_hash": b"f" * 32, "rejection_code": "QR_EXPIRED",
             "scan_authorization_id": 99, "scan_bound_at": datetime(2026, 9, 9)},
            {"outcome": "RECEIVED"},
        ]
        with (
            patch.object(self.service, "_receive", return_value=self.receipt),
            patch("meal_management.meals.require_scan_actor", return_value=Actor(42, frozenset({"WAITER"}))),
        ):
            result = self.service.record(self.context, self.scan)
        self.assertFalse(result.approved)
        self.assertFalse(result.duplicate)
        self.assertEqual(result.code, "IDEMPOTENCY_KEY_REUSED")

    def test_legacy_rejection_without_proof_does_not_claim_verified_replay(self):
        self.transaction.one.side_effect = [
            {"status": "REJECTED", "payload_hash": b"f" * 32, "rejection_code": "QR_EXPIRED"},
            {"outcome": "RECEIVED"},
        ]
        with (
            patch.object(self.service, "_receive", return_value=self.receipt),
            patch("meal_management.meals.require_scan_actor", return_value=Actor(42, frozenset({"WAITER"}))),
        ):
            result = self.service.record(self.context, self.scan)
        self.assertEqual(result.code, "IDEMPOTENCY_PROOF_UNAVAILABLE")
        self.assertFalse(result.duplicate)

    def test_changed_authenticated_identity_cannot_replay(self):
        self.transaction.one.side_effect = [
            {"status": "SUCCEEDED", "payload_hash": b"f" * 32},
            {"outcome": "RECEIVED"},
        ]
        with (
            patch.object(self.service, "_receive", return_value=self.receipt),
            patch("meal_management.meals.require_scan_actor", return_value=Actor(43, frozenset({"WAITER"}))),
        ):
            result = self.service.record(self.context, self.scan)
        self.assertEqual(result.code, "AUTHENTICATION_CHANGED")
        self.transaction.all.assert_not_called()

    def test_successful_retry_with_changed_authorization_cannot_replay(self):
        self.transaction.one.side_effect = [
            {"status": "SUCCEEDED", "payload_hash": b"f" * 32},
            {"outcome": "RECEIVED"},
            {"id": 77, "qr_id": 12, "authorization_id": 99},
        ]
        with (
            patch.object(self.service, "_receive", return_value=self.receipt),
            patch("meal_management.meals.require_scan_actor", return_value=Actor(42, frozenset({"WAITER"}))),
        ):
            result = self.service.record(self.context, self.scan)
        self.assertEqual(result.code, "IDEMPOTENCY_KEY_REUSED")
        self.transaction.all.assert_not_called()

    def test_transient_receipt_deadlock_retries_same_serving_identifier(self):
        deadlock = RuntimeError("intentional deadlock")
        deadlock.errno = 1213
        result = ScanResult(True, "APPROVED", str(self.identifier), 77, (91,))
        with (
            patch.object(self.service, "_receive", side_effect=[deadlock, self.receipt]) as receive,
            patch.object(self.service, "_process", return_value=result) as process,
            patch("meal_management.meals.time.sleep"),
        ):
            actual = self.service.record(self.context, self.scan)
        self.assertIs(actual, result)
        self.assertEqual(receive.call_count, 2)
        self.assertTrue(all(call.args[1].request_id == self.identifier for call in receive.call_args_list))
        process.assert_called_once_with(self.context, self.scan, self.receipt)

    def test_transient_processing_deadlock_reuses_receipt(self):
        deadlock = RuntimeError("intentional deadlock")
        deadlock.errno = 1213
        result = ScanResult(True, "APPROVED", str(self.identifier), 77, (91,))
        with (
            patch.object(self.service, "_receive", return_value=self.receipt) as receive,
            patch.object(self.service, "_process", side_effect=[deadlock, result]) as process,
            patch.object(self.service, "_mark_interrupted") as interrupted,
            patch("meal_management.meals.time.sleep"),
        ):
            actual = self.service.record(self.context, self.scan)
        self.assertIs(actual, result)
        receive.assert_called_once()
        self.assertEqual(process.call_count, 2)
        interrupted.assert_not_called()


class ServingInputTests(unittest.TestCase):
    def test_fingerprint_binds_token_caller_scanner_location_type_and_quantity(self):
        original = (b"t" * 32, 42, 3, 4, 5, 1)
        digest = scan_fingerprint(*original)
        for index, changed in enumerate((b"s" * 32, 43, 4, 5, 6, 2)):
            with self.subTest(index=index):
                values = list(original)
                values[index] = changed
                self.assertNotEqual(scan_fingerprint(*values), digest)

    def test_equivalent_uuid_encodings_have_one_identifier(self):
        identifier = uuid4()
        self.assertEqual(request_uuid(identifier), request_uuid(str(identifier).upper()))

    def test_invalid_request_identifiers_are_rejected(self):
        for value in (None, "", "employee-001", "00000000-0000-0000-0000-000000000000"):
            with self.subTest(value=value):
                with self.assertRaises(DomainError):
                    request_uuid(value)

    def test_booleans_and_nonpositive_quantities_are_rejected(self):
        for quantity in (True, False, 0, -1, 1.0, "1", 65536):
            with self.subTest(quantity=quantity):
                with self.assertRaises(DomainError):
                    positive_integer(quantity, "QUANTITY", 65535)


class PreparedAuthorizationTests(unittest.TestCase):
    def test_missing_or_wrong_proof_rejects_only_attempt(self):
        identifier = uuid4()
        service = MealService(Mock())
        actor = Actor(42, frozenset({"WAITER"}))
        for authorization_id in (None, 52):
            with self.subTest(authorization_id=authorization_id):
                transaction = Mock()
                transaction.one.return_value = {"id": 51}
                scan = ScanInput(identifier, generate_token(), 1, "SCANNER", 2, authorization_id)
                with self.assertRaises(AttemptRejection) as caught:
                    service._validate_authorization(
                        transaction, actor, scan, {"request_id": identifier.bytes},
                        {"id": 12}, {"id": 3, "location_id": 4},
                    )
                self.assertEqual(caught.exception.code, "AUTHORIZATION_SCOPE_MISMATCH")
                transaction.one.assert_called_once()
                transaction.execute.assert_not_called()

    def test_rejected_proof_keeps_prepared_request_pending(self):
        identifier = uuid4()
        transaction = MagicMock()
        request = {"status": "PENDING", "payload_hash": b"f" * 32}
        transaction.one.side_effect = [request, {"outcome": "RECEIVED"}, {"id": 51}]
        transaction.execute.return_value = 1
        database = MagicMock()
        database.transaction.return_value.__enter__.return_value = transaction
        service = MealService(database)
        receipt = {
            "attempt_id": 9,
            "request_id": identifier.bytes,
            "request_text": str(identifier),
            "fingerprint": b"f" * 32,
            "digest": b"d" * 32,
            "staff_id": 42,
            "error": None,
        }
        scan = ScanInput(identifier, generate_token(), 1, "SCANNER", 2)
        with (
            patch.object(service, "_receive", return_value=receipt),
            patch.object(service, "_create_serving", side_effect=AttemptRejection("AUTHORIZATION_SCOPE_MISMATCH")),
            patch.object(service, "_mark_interrupted") as interrupted,
            patch("meal_management.meals.require_scan_actor", return_value=Actor(42, frozenset({"WAITER"}))),
        ):
            result = service.record(ServerContext(generate_token()), scan)
        self.assertFalse(result.approved)
        self.assertEqual(result.code, "AUTHORIZATION_SCOPE_MISMATCH")
        sql_statements = [call.args[0] for call in transaction.execute.call_args_list]
        self.assertFalse(any(statement.startswith("UPDATE serving_requests") for statement in sql_statements))
        self.assertTrue(any(statement.startswith("UPDATE scan_attempts") for statement in sql_statements))
        transaction.insert.assert_not_called()
        interrupted.assert_not_called()


class InvalidScanReceiptTests(unittest.TestCase):
    def test_invalid_authenticated_scan_is_logged_without_credential_data(self):
        database = MagicMock()
        tx = database.transaction.return_value.__enter__.return_value
        tx.one.return_value = {"id": 3, "location_id": 4}
        tx.now.return_value = datetime(2026, 9, 9)
        context = ServerContext(generate_token())
        with patch("meal_management.meals.require_scan_actor", return_value=Actor(42, frozenset({"WAITER"}))):
            result = MealService(database).record_invalid(context, "SCANNER")
        self.assertEqual(result.code, "INVALID_INPUT")
        sql, params = tx.insert.call_args.args
        self.assertNotIn("token", sql)
        self.assertNotIn("payload", sql)
        self.assertNotIn("request_id", sql)
        self.assertEqual(params[:4], (42, 3, 4, "SCANNER"))

    def test_invalid_scan_requires_waiter_authentication(self):
        database = MagicMock()
        tx = database.transaction.return_value.__enter__.return_value
        with patch("meal_management.meals.require_scan_actor", side_effect=DomainError("ROLE_REQUIRED")):
            with self.assertRaises(DomainError):
                MealService(database).record_invalid(ServerContext(generate_token()))
        tx.insert.assert_not_called()

    def test_invalid_scan_receipt_commit_failure_propagates(self):
        tx = Mock()
        db = FailingCommitDatabase(tx)
        with patch("meal_management.meals.require_scan_actor", return_value=Actor(42, frozenset({"WAITER"}))):
            with self.assertRaisesRegex(RuntimeError, "intentional commit failure"):
                MealService(db).record_invalid(ServerContext(generate_token()))
        self.assertTrue(db.commit_attempted)


class ServingCreationTests(unittest.TestCase):
    def setUp(self):
        self.tx = Mock()
        self.now = datetime(2026, 9, 9, 12)
        self.tx.now.return_value = self.now
        self.tx.execute.return_value = 1
        self.service = MealService(Mock())
        self.actor = Actor(42, frozenset({"WAITER"}))
        self.identifier = uuid4()
        self.receipt = {
            "request_id": self.identifier.bytes, "request_text": str(self.identifier),
            "attempt_id": 9, "digest": b"d" * 32, "scanner_id": 3, "location_id": 4,
        }
        self.scanner = {"id": 3, "location_id": 4, "is_active": True, "location_active": True}

    def test_employee_serving_records_exactly_one_meal_with_actor_and_time(self):
        qr = {"id": 12, "kind": "EMPLOYEE", "employee_id": 21, "revoked_at": None, "expires_at": None}
        self.tx.one.side_effect = [self.scanner, {"id": 1, "is_active": True}, qr, {"id": 21, "is_active": True}, qr]
        self.tx.insert.side_effect = [77, 91]
        result = self.service._create_serving(
            self.tx, self.actor, ScanInput(self.identifier, generate_token(), 1, "SCANNER"), self.receipt
        )
        self.assertTrue(result.approved)
        self.assertEqual(result.meal_ids, (91,))
        serving = self.tx.insert.call_args_list[0].args[1]
        self.assertEqual(serving[2:4], ("EMPLOYEE", 21))
        self.assertEqual(serving[6], 42)
        self.assertEqual(serving[-1], self.now)
        self.assertEqual(self.tx.insert.call_args_list[1].args[1], (77, 1, 1, self.now))

    def test_master_serving_records_each_authorized_unit(self):
        qr = {"id": 12, "kind": "MASTER", "employee_id": None, "revoked_at": None, "expires_at": None}
        approval = {"request_id": self.identifier.bytes, "qr_id": 12, "meal_type_id": 1, "quantity": 3,
                    "waiter_id": 42, "scanner_id": 3, "location_id": 4, "authorized_by": 7,
                    "revoked_at": None, "expires_at": self.now + timedelta(minutes=5)}
        self.tx.one.side_effect = [self.scanner, {"id": 1, "is_active": True}, qr, qr, None,
                                   {"id": 51}, approval, {"is_active": True}, {"role_code": "ADMIN"}]
        self.tx.insert.side_effect = [77, 91, 92, 93]
        result = self.service._create_serving(
            self.tx, self.actor, ScanInput(self.identifier, generate_token(), 1, "SCANNER", 3, 51), self.receipt
        )
        self.assertTrue(result.approved)
        self.assertEqual(result.meal_ids, (91, 92, 93))
        serving = self.tx.insert.call_args_list[0].args[1]
        self.assertEqual(serving[2:4], ("MASTER", None))
        self.assertEqual(serving[-2], 51)
        self.assertEqual([call.args[1][1] for call in self.tx.insert.call_args_list[1:]], [1, 2, 3])

    def test_employee_batch_quantity_is_rejected_before_insert(self):
        qr = {"id": 12, "kind": "EMPLOYEE", "employee_id": 21, "revoked_at": None, "expires_at": None}
        self.tx.one.side_effect = [self.scanner, {"id": 1, "is_active": True}, qr, {"id": 21, "is_active": True}, qr]
        with self.assertRaises(DomainError) as caught:
            self.service._create_serving(
                self.tx, self.actor, ScanInput(self.identifier, generate_token(), 1, "SCANNER", 2), self.receipt
            )
        self.assertEqual(caught.exception.code, "EMPLOYEE_QUANTITY_MUST_BE_ONE")
        self.tx.insert.assert_not_called()


class MealReceiptBindingTests(unittest.TestCase):
    def setUp(self):
        self.db = MagicMock()
        self.tx = self.db.transaction.return_value.__enter__.return_value
        self.tx.insert.return_value = 77
        self.tx.now.return_value = datetime(2026, 9, 9, 12)
        self.service = MealService(self.db)
        self.context = ServerContext(generate_token())
        self.scan = ScanInput(uuid4(), generate_token(), 1, "SCANNER")
        self.original_fingerprint = scan_fingerprint(token_digest(self.scan.token), 42, 3, 4, 1, 1)
        self.actor = patch("meal_management.meals.require_scan_actor", return_value=Actor(42, frozenset({"WAITER"})))
        self.actor.start()
        self.addCleanup(self.actor.stop)

    def test_completed_retry_uses_original_location_but_logs_current_location(self):
        self.tx.one.side_effect = [
            {"id": 3, "location_id": 5},
            {"status": "SUCCEEDED", "payload_hash": self.original_fingerprint},
            {"scanner_id": 3, "location_id": 4, "reported_scanner_code": "SCANNER"},
        ]
        receipt = self.service._receive(self.context, self.scan)
        self.assertEqual(receipt["fingerprint"], self.original_fingerprint)
        self.assertEqual(receipt["location_id"], 5)
        self.assertIsNone(receipt["error"])
        params = self.tx.insert.call_args.args[1]
        self.assertEqual(params[4:7], (3, 5, "SCANNER"))

    def test_pending_approval_uses_current_location_and_cannot_match_old_scope(self):
        self.tx.one.side_effect = [
            {"id": 3, "location_id": 5},
            {"status": "PENDING", "payload_hash": self.original_fingerprint},
        ]
        receipt = self.service._receive(self.context, self.scan)
        self.assertNotEqual(receipt["fingerprint"], self.original_fingerprint)
        self.assertEqual(self.tx.one.call_count, 2)

    def test_changed_scanner_code_cannot_replay_even_if_device_identity_matches(self):
        self.tx.one.side_effect = [
            {"id": 3, "location_id": 4},
            {"status": "SUCCEEDED", "payload_hash": self.original_fingerprint},
            {"scanner_id": 3, "location_id": 4, "reported_scanner_code": "ORIGINAL-CODE"},
        ]
        receipt = self.service._receive(self.context, self.scan)
        self.assertEqual(receipt["error"], "IDEMPOTENCY_KEY_REUSED")
        self.assertEqual(self.tx.insert.call_args.args[1][7:9], ("REJECTED", "IDEMPOTENCY_KEY_REUSED"))

    def test_original_receipt_does_not_override_submitted_token(self):
        original_digest = scan_fingerprint(b"d" * 32, 42, 3, 4, 1, 1)
        self.tx.one.side_effect = [
            {"id": 3, "location_id": 5},
            {"status": "SUCCEEDED", "payload_hash": original_digest},
            {"scanner_id": 3, "location_id": 4, "reported_scanner_code": "SCANNER"},
        ]
        receipt = self.service._receive(self.context, self.scan)
        self.assertNotEqual(receipt["fingerprint"], original_digest)

    def test_missing_original_receipt_cannot_claim_verified_replay(self):
        self.tx.one.side_effect = [
            {"id": 3, "location_id": 5},
            {"status": "SUCCEEDED", "payload_hash": self.original_fingerprint},
            None,
        ]
        receipt = self.service._receive(self.context, self.scan)
        self.assertEqual(receipt["error"], "IDEMPOTENCY_PROOF_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
