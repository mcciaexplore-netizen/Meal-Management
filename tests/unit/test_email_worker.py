import copy
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import Mock

from meal_management.delivery import DeliveryError
from meal_management.email_worker import EmailDeliveryWorker
from meal_management.errors import DomainError
from meal_management.security import generate_token, token_digest


class WorkerTransaction:
    def __init__(self, database):
        self.database = database
        self.queue = copy.deepcopy(database.queue)
        self.locks = []

    def now(self):
        return self.database.now

    def one(self, sql, params=()):
        self.database.queries.append((sql, params))
        if sql.startswith("SELECT c.employee_id"):
            return {"employee_id": 7, "qr_id": 8} if self.database.exists else None
        if "FROM employees" in sql:
            self.locks.append("employee")
            return copy.deepcopy(self.database.employee)
        if "FROM qr_credentials" in sql:
            self.locks.append("qr")
            return copy.deepcopy(self.database.credential)
        if "FROM email_queue" in sql:
            self.locks.append("queue")
            return copy.deepcopy(self.queue)
        raise AssertionError("Unexpected query")

    def execute(self, sql, params=()):
        self.database.queries.append((sql, params))
        status, code, _, clear, identifier, expected_status, expected_error = params
        if self.database.update_fails:
            raise RuntimeError("private database details")
        if self.queue["id"] != identifier or self.queue["status"] != expected_status or self.queue["last_error"] != expected_error:
            return 0
        self.queue.update(status=status, last_error=code)
        if clear in {"SENT", "CANCELLED"}:
            self.queue["payload_ciphertext"] = None
        return 1


class WorkerDatabase:
    def __init__(self, token):
        self.now = datetime(2026, 9, 9, 12)
        self.employee = {"id": 7, "email": "fictional-employee@gmail.com", "is_active": True}
        self.credential = {"token_hash": token_digest(token), "revoked_at": None, "expires_at": None}
        self.queue = {"id": 9, "recipient_email": self.employee["email"], "payload_ciphertext": b"encrypted", "status": "QUEUED", "last_error": None}
        self.exists = True
        self.transactions = 0
        self.commits = 0
        self.fail_commit = None
        self.persist_failed_commit = False
        self.update_fails = False
        self.after_claim = None
        self.active = None
        self.queries = []
        self.lock = threading.RLock()

    @contextmanager
    def transaction(self):
        with self.lock:
            self.transactions += 1
            number = self.transactions
            tx = WorkerTransaction(self)
            self.active = tx
            try:
                yield tx
                if self.fail_commit == number and not self.persist_failed_commit:
                    raise RuntimeError("private commit details")
                self.queue = tx.queue
                self.commits += 1
                if self.fail_commit == number:
                    raise RuntimeError("private lost acknowledgement")
                if number == 1 and self.after_claim:
                    self.after_claim(self)
            finally:
                self.active = None


class EmailWorkerTests(unittest.TestCase):
    def setUp(self):
        self.token = generate_token()
        self.db = WorkerDatabase(self.token)
        self.vault = Mock()
        self.payload = {"employee_id": 7, "employee_name": "Fictional Employee", "token": self.token, "qr_svg": "untrusted stored SVG"}
        self.vault.decrypt.side_effect = lambda _: json.dumps(self.payload)
        self.renderer = Mock()
        self.delivery = Mock(enabled=True)
        self.delivery.send.return_value = "<safe-message-id>"
        self.worker = EmailDeliveryWorker(self.db, self.vault, self.renderer, self.delivery)

    def send(self):
        return self.worker.send(9, allow_real_email=True)

    def test_explicit_approval_and_enabled_adapter_are_required_before_database_access(self):
        for enabled, approved in ((False, True), (True, False), (False, False), (True, 1)):
            self.delivery.enabled = enabled
            with self.assertRaisesRegex(DomainError, "REAL_EMAIL_NOT_AUTHORIZED"):
                self.worker.send(9, allow_real_email=approved)
        self.assertEqual(self.db.transactions, 0)
        self.delivery.send.assert_not_called()

    def test_only_a_positive_specific_identifier_is_accepted(self):
        for identifier in (None, True, 0, -1, "9", 2**64):
            with self.assertRaisesRegex(DomainError, "INVALID_EMAIL_ID"):
                self.worker.send(identifier, allow_real_email=True)
        self.assertEqual(self.db.transactions, 0)

    def test_claim_is_committed_before_send_and_validity_locks_remain_held(self):
        def deliver(preview, **options):
            self.assertEqual(self.db.commits, 1)
            self.assertEqual(self.db.queue["status"], "FAILED")
            self.assertTrue(self.db.queue["last_error"].startswith("EMAIL_DELIVERY_CLAIMED_"))
            self.assertEqual(self.db.active.locks, ["employee", "qr", "queue"])
            self.assertEqual(preview.token, self.token)
            self.assertEqual(preview.qr_svg, "")
            self.assertNotIn(self.token, repr(preview))
            self.assertEqual(options, {"allow_real_email": True})
            return "<accepted>"
        self.delivery.send.side_effect = deliver
        self.assertEqual(self.send(), {"email_id": 9, "status": "SENT", "code": None})
        self.assertEqual(self.db.commits, 2)
        self.assertEqual(self.db.queue["status"], "SENT")
        self.assertIsNone(self.db.queue["payload_ciphertext"])
        self.renderer.render.assert_not_called()

    def test_successful_replay_never_sends_again_even_after_revocation(self):
        self.send()
        self.db.credential["revoked_at"] = self.db.now
        self.assertEqual(self.send()["status"], "ALREADY_SENT")
        self.delivery.send.assert_called_once()

    def test_failed_claim_commit_never_calls_provider(self):
        for ambiguous in (False, True):
            with self.subTest(ambiguous=ambiguous):
                self.setUp()
                self.db.fail_commit = 1
                self.db.persist_failed_commit = ambiguous
                with self.assertRaisesRegex(DomainError, "EMAIL_CLAIM_UNCONFIRMED"):
                    self.send()
                self.delivery.send.assert_not_called()
                self.assertEqual(self.db.queue["status"], "FAILED" if ambiguous else "QUEUED")

    def test_acknowledgement_commit_failure_never_returns_sent_or_blindly_retries(self):
        for ambiguous in (False, True):
            with self.subTest(ambiguous=ambiguous):
                self.setUp()
                self.db.fail_commit = 2
                self.db.persist_failed_commit = ambiguous
                self.assertEqual(self.send()["status"], "NEEDS_REVIEW")
                self.db.fail_commit = None
                self.assertEqual(self.send()["status"], "ALREADY_SENT" if ambiguous else "NEEDS_REVIEW")
                self.delivery.send.assert_called_once()

    def test_simultaneous_workers_send_at_most_once(self):
        barrier = threading.Barrier(2)
        def run():
            barrier.wait(timeout=2)
            return self.send()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: run(), range(2)))
        self.assertEqual(sum(result["status"] == "SENT" for result in results), 1)
        self.assertTrue(all(result["status"] in {"SENT", "ALREADY_SENT", "NEEDS_REVIEW"} for result in results))
        self.delivery.send.assert_called_once()

    def test_stale_employee_and_qr_states_cancel_without_provider_access(self):
        changes = (
            lambda db: db.employee.update(is_active=False),
            lambda db: db.employee.update(email="changed@gmail.com"),
            lambda db: db.credential.update(revoked_at=db.now),
            lambda db: db.credential.update(expires_at=db.now),
            lambda db: db.credential.update(expires_at=db.now - timedelta(seconds=1)),
            lambda db: setattr(db, "employee", None),
            lambda db: setattr(db, "credential", None),
        )
        for change in changes:
            self.setUp()
            change(self.db)
            self.assertEqual(self.send()["status"], "CANCELLED")
            self.assertIsNone(self.db.queue["payload_ciphertext"])
            self.delivery.send.assert_not_called()

    def test_state_is_revalidated_after_durable_claim_before_provider_access(self):
        for change in (
            lambda db: db.employee.update(is_active=False),
            lambda db: db.employee.update(email="changed@gmail.com"),
            lambda db: db.credential.update(revoked_at=db.now),
            lambda db: db.credential.update(expires_at=db.now),
            lambda db: db.credential.update(token_hash=token_digest(generate_token())),
        ):
            self.setUp()
            self.db.after_claim = change
            self.assertIn(self.send()["status"], {"CANCELLED", "FAILED"})
            self.delivery.send.assert_not_called()

    def test_reserved_test_recipients_are_cancelled_before_provider_access(self):
        for domain in ("example.com", "example.org", "example.net", "example.test", "system.invalid", "localhost"):
            self.setUp()
            self.db.employee["email"] = "fictional@" + domain
            self.db.queue["recipient_email"] = self.db.employee["email"]
            self.assertEqual(self.send()["status"], "CANCELLED")
            self.delivery.send.assert_not_called()

    def test_tampered_or_unreadable_payloads_are_failed_without_rendering_or_sending(self):
        for change in (
            lambda: self.payload.update(employee_id=99),
            lambda: self.payload.update(token=generate_token()),
            lambda: self.payload.update(token="invalid"),
            lambda: self.payload.update(employee_name="name\nheader"),
            lambda: setattr(self.vault.decrypt, "side_effect", DomainError("ENCRYPTED_PAYLOAD_UNREADABLE")),
        ):
            self.setUp()
            change()
            result = self.send()
            self.assertEqual(result["status"], "FAILED")
            self.assertEqual(result["code"], "INVALID_EMAIL_PAYLOAD")
            self.delivery.send.assert_not_called()
            self.renderer.render.assert_not_called()

    def test_missing_stored_svg_is_ignored_for_live_delivery(self):
        del self.payload["qr_svg"]
        self.assertEqual(self.send()["status"], "SENT")

    def test_definite_provider_failure_is_safe_and_cannot_be_retried(self):
        self.delivery.send.side_effect = DeliveryError("EMAIL_AUTHENTICATION_FAILED", uncertain=False)
        expected = {"email_id": 9, "status": "FAILED", "code": "EMAIL_AUTHENTICATION_FAILED"}
        self.assertEqual(self.send(), expected)
        self.assertEqual(self.send(), expected)
        self.delivery.send.assert_called_once()

    def test_uncertain_provider_outcome_never_returns_success_or_retries(self):
        for failure in (DeliveryError("EMAIL_DELIVERY_UNCONFIRMED", uncertain=True), RuntimeError("private provider details")):
            self.setUp()
            self.delivery.send.side_effect = failure
            self.assertEqual(self.send()["status"], "NEEDS_REVIEW")
            self.assertEqual(self.send()["status"], "NEEDS_REVIEW")
            self.assertEqual(self.db.queue["last_error"], "EMAIL_DELIVERY_NEEDS_REVIEW")
            self.delivery.send.assert_called_once()

    def test_unknown_provider_codes_cannot_be_stored_or_returned(self):
        self.delivery.send.side_effect = DeliveryError("private recipient and provider details", uncertain=False)
        result = self.send()
        self.assertEqual(result["code"], "EMAIL_DELIVERY_FAILED")
        self.assertEqual(self.db.queue["last_error"], "EMAIL_DELIVERY_FAILED")

    def test_unverified_provider_acknowledgement_requires_review(self):
        for acknowledgement in (None, "", False, {"accepted": True}):
            self.setUp()
            self.delivery.send.return_value = acknowledgement
            self.assertEqual(self.send()["status"], "NEEDS_REVIEW")

    def test_claim_changed_or_cancelled_between_transactions_cannot_send(self):
        for status, marker in (("CANCELLED", None), ("FAILED", "different claim"), ("QUEUED", None)):
            self.setUp()
            self.db.after_claim = lambda db: db.queue.update(status=status, last_error=marker)
            self.assertNotEqual(self.send()["status"], "SENT")
            self.delivery.send.assert_not_called()

    def test_existing_failed_rows_never_send_and_hide_stored_provider_details(self):
        self.db.queue.update(status="FAILED", last_error="sensitive preexisting provider failure")
        self.assertEqual(self.send(), {"email_id": 9, "status": "FAILED", "code": "EMAIL_DELIVERY_FAILED"})
        self.delivery.send.assert_not_called()

    def test_missing_email_returns_safe_not_found(self):
        self.db.exists = False
        with self.assertRaisesRegex(DomainError, "EMAIL_NOT_FOUND"):
            self.send()
        self.delivery.send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
