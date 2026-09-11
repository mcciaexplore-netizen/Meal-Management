import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch
from uuid import uuid4

from meal_management.email_queue import EmailQueueService
from meal_management.employees import EmployeeService, Registration
from meal_management.errors import DomainError
from meal_management.models import Actor, ServerContext
from meal_management.qr import QrService
from meal_management.queries import QueryService
from meal_management.security import generate_token, payload_digest, token_digest
from test_account_services import TransactionDatabase


class EmailPolicyTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 10, 12)
        self.tx = Mock()
        self.tx.now.return_value = self.now
        self.tx.execute.return_value = 1
        self.db = TransactionDatabase(self.tx)
        self.vault = Mock()
        self.renderer = Mock(render=Mock(return_value="<svg/>"))
        self.queue = EmailQueueService(self.db, self.vault, self.renderer)
        self.actor = Actor(7, frozenset({"ADMIN"}))
        self.context = ServerContext(generate_token())
        self.token = generate_token()
        self.employee = {"id": 1, "email": "employee@example.test", "is_active": True, "full_name": "Test Employee"}
        self.credential = {"id": 2, "token_hash": token_digest(self.token), "revoked_at": None, "expires_at": None}
        self.row = {"id": 3, "recipient_email": self.employee["email"], "status": "DRAFT", "delivery_mode": "SINGLE", "bulk_batch_id": None, "approved_by_staff_id": None, "approved_at": None, "payload_ciphertext": b"encrypted", "last_error": None}
        self.vault.decrypt.return_value = json.dumps({"employee_id": 1, "employee_name": "Test Employee", "token": self.token, "qr_svg": "<svg/>"})
        self.actor_patch = patch("meal_management.email_queue.require_actor", return_value=self.actor)
        self.actor_patch.start()
        self.addCleanup(self.actor_patch.stop)

    def lock_rows(self, rows=None):
        rows = rows or [(self.employee, self.credential, self.row)]
        self.tx.all.return_value = [{"id": row[2]["id"]} for row in rows]
        self.queue.lock_email = Mock(side_effect=rows)

    def bulk(self, status="PENDING_APPROVAL", approved=False):
        self.row.update(delivery_mode="BULK", bulk_batch_id=8, status=status)
        if approved:
            self.row.update(approved_by_staff_id=7, approved_at=self.now)

    def test_enqueue_defaults_to_private_single_draft(self):
        self.queue.enqueue_employee(self.tx, 2, self.employee, self.token)
        sql, values = self.tx.insert.call_args.args
        self.assertEqual(values[3], "DRAFT")
        self.assertEqual(values[6:], ("SINGLE", None))
        self.assertIn("delivery_mode, bulk_batch_id", sql)

    def test_bulk_enqueue_waits_for_explicit_approval(self):
        self.queue.enqueue_employee(self.tx, 2, self.employee, self.token, bulk_batch_id=8)
        values = self.tx.insert.call_args.args[1]
        self.assertEqual(values[3], "PENDING_APPROVAL")
        self.assertEqual(values[6:], ("BULK", 8))

    def test_admin_preview_supports_unapproved_drafts_without_approving(self):
        for status in ("DRAFT", "PENDING_APPROVAL", "QUEUED"):
            self.row["status"] = status
            self.lock_rows()
            result = self.queue.preview(self.context, 3)
            self.assertEqual(result.token, self.token)
        self.tx.execute.assert_not_called()

    def test_single_approval_stamps_authenticated_admin_and_commits_without_sending(self):
        self.lock_rows()
        self.assertEqual(self.queue.approve_single(self.context, 3, 1), 3)
        sql, values = self.tx.execute.call_args.args
        self.assertIn("status = 'QUEUED'", sql)
        self.assertEqual(values, (7, self.now, self.now, 3, "DRAFT"))
        self.assertTrue(self.db.committed)

    def test_single_approval_retries_preserve_original_stamp_and_final_result(self):
        for status in ("QUEUED", "SENT", "FAILED", "CANCELLED"):
            self.row.update(status=status, approved_by_staff_id=9, approved_at=self.now - timedelta(minutes=1))
            self.lock_rows()
            self.assertEqual(self.queue.approve_single(self.context, 3, 1), 3)
        self.tx.execute.assert_not_called()

    def test_single_approval_rejects_wrong_employee_or_bulk_mode(self):
        self.lock_rows()
        with self.assertRaisesRegex(DomainError, "EMAIL_NOT_FOUND"):
            self.queue.approve_single(self.context, 3, 99)
        self.bulk()
        self.lock_rows()
        with self.assertRaisesRegex(DomainError, "SINGLE_EMAIL_REQUIRED"):
            self.queue.approve_single(self.context, 3, 1)
        self.tx.execute.assert_not_called()

    def test_single_approval_failure_does_not_return_success(self):
        self.lock_rows()
        self.db.commit_error = RuntimeError("commit failed")
        with self.assertRaisesRegex(RuntimeError, "commit failed"):
            self.queue.approve_single(self.context, 3, 1)
        self.assertTrue(self.db.rolled_back)

    def test_approval_rejects_stale_employee_qr_or_recipient(self):
        for mutation in (
            lambda: self.employee.update(is_active=False),
            lambda: self.employee.update(email="changed@example.test"),
            lambda: self.credential.update(revoked_at=self.now),
            lambda: self.credential.update(expires_at=self.now),
        ):
            old_employee, old_credential = dict(self.employee), dict(self.credential)
            mutation()
            self.lock_rows()
            with self.assertRaisesRegex(DomainError, "EMAIL_NOT_DELIVERABLE"):
                self.queue.approve_single(self.context, 3, 1)
            self.employee, self.credential = old_employee, old_credential
        self.tx.execute.assert_not_called()

    def test_bulk_approval_validates_every_row_before_any_stamp(self):
        self.bulk()
        stale_employee = {**self.employee, "is_active": False}
        second = {**self.row, "id": 4}
        self.lock_rows([(self.employee, self.credential, self.row), (stale_employee, self.credential, second)])
        with self.assertRaisesRegex(DomainError, "EMAIL_NOT_DELIVERABLE"):
            self.queue.approve_bulk(self.context, [3, 4])
        self.tx.execute.assert_not_called()
        self.assertTrue(self.db.rolled_back)

    def test_bulk_approval_counts_only_new_approvals_and_keeps_requested_order(self):
        self.bulk()
        second = {**self.row, "id": 4, "status": "SENT", "approved_by_staff_id": 9, "approved_at": self.now}
        self.lock_rows([(self.employee, self.credential, self.row), (self.employee, self.credential, second)])
        result = self.queue.approve_bulk(self.context, [4, 3])
        self.assertEqual(result, {"email_ids": [4, 3], "approved_count": 1})
        self.tx.execute.assert_called_once()
        self.assertIn("ORDER BY c.employee_id, c.id, q.id", self.tx.all.call_args.args[0])

    def test_legacy_pending_mail_requires_and_can_receive_admin_approval(self):
        self.row.update(delivery_mode="LEGACY", status="PENDING_APPROVAL")
        self.lock_rows()
        self.assertEqual(self.queue.approve_bulk(self.context, [3])["approved_count"], 1)

    def test_bulk_approval_cannot_include_single_email(self):
        self.lock_rows()
        with self.assertRaisesRegex(DomainError, "BULK_EMAIL_REQUIRED"):
            self.queue.approve_bulk(self.context, [3])
        self.tx.execute.assert_not_called()

    def test_all_bulk_ids_must_exist_before_locks_or_updates(self):
        self.bulk()
        self.lock_rows()
        with self.assertRaisesRegex(DomainError, "EMAIL_NOT_FOUND"):
            self.queue.approve_bulk(self.context, [3, 99])
        self.queue.lock_email.assert_not_called()
        self.tx.execute.assert_not_called()

    def test_process_validation_requires_all_ids_approved_and_bulk(self):
        self.bulk()
        self.lock_rows()
        with self.assertRaisesRegex(DomainError, "EMAIL_APPROVAL_REQUIRED"):
            self.queue.validate_approved_bulk(self.context, [3])
        self.bulk("QUEUED", approved=True)
        self.lock_rows()
        self.assertEqual(self.queue.validate_approved_bulk(self.context, [3]), [3])
        self.tx.execute.assert_not_called()

    def test_process_validation_allows_previously_approved_final_results_without_reopening(self):
        for status in ("SENT", "FAILED", "CANCELLED"):
            self.bulk(status, approved=True)
            self.employee["is_active"] = False
            self.lock_rows()
            self.assertEqual(self.queue.validate_approved_bulk(self.context, [3]), [3])
        self.tx.execute.assert_not_called()

    def test_process_validation_leaves_expired_approved_email_for_worker_cancellation(self):
        self.bulk("QUEUED", approved=True)
        self.credential["expires_at"] = self.now
        self.lock_rows()
        self.assertEqual(self.queue.validate_approved_bulk(self.context, [3]), [3])
        self.vault.decrypt.assert_not_called()
        self.tx.execute.assert_not_called()

    def test_limits_duplicates_and_invalid_ids_are_rejected_before_database_access(self):
        for identifiers in ([], [True], [0], [3, 3], "3", list(range(1, 102))):
            with self.assertRaisesRegex(DomainError, "INVALID_EMAIL_IDS"):
                self.queue.approve_bulk(self.context, identifiers)
        with self.assertRaisesRegex(DomainError, "INVALID_EMAIL_IDS"):
            self.queue.validate_approved_bulk(self.context, list(range(1, 12)))
        self.assertFalse(self.db.committed)

    def test_new_service_actions_require_admin_authorization(self):
        with patch("meal_management.email_queue.require_actor", side_effect=DomainError("ROLE_REQUIRED")):
            for action in (
                lambda: self.queue.approve_single(self.context, 3, 1),
                lambda: self.queue.approve_bulk(self.context, [3]),
                lambda: self.queue.validate_approved_bulk(self.context, [3]),
                lambda: self.queue.status(self.context, 3),
            ):
                with self.assertRaisesRegex(DomainError, "ROLE_REQUIRED"):
                    action()
        self.tx.one.assert_not_called()

    def test_direct_status_is_employee_scoped_safe_and_utc(self):
        self.tx.one.return_value = {**self.row, "employee_id": 1, "status": "FAILED", "last_error": "EMAIL_DELIVERY_CLAIMED_private", "approved_at": self.now}
        result = self.queue.status(self.context, 3, employee_id=1)
        self.assertEqual(result["status"], "NEEDS_REVIEW")
        self.assertEqual(result["code"], "EMAIL_DELIVERY_NEEDS_REVIEW")
        self.assertEqual(result["approved_at"].tzinfo, timezone.utc)
        self.assertNotIn("private", repr(result))
        self.assertNotIn("recipient_email", result)
        with self.assertRaisesRegex(DomainError, "EMAIL_NOT_FOUND"):
            self.queue.status(self.context, 3, employee_id=2)

    def test_bulk_list_scope_excludes_single_and_normalizes_review_state(self):
        self.tx.all.return_value = [{"id": 3, "status": "FAILED", "last_error": "provider secrets"}]
        with patch("meal_management.queries.require_actor", return_value=self.actor):
            result = QueryService(self.db).email_status(self.context, delivery_scope="bulk")
        self.assertIn("q.delivery_mode IN ('BULK', 'LEGACY')", self.tx.all.call_args.args[0])
        self.assertEqual(result["items"][0]["status"], "NEEDS_REVIEW")
        self.assertNotIn("secrets", repr(result))

    def test_cancellation_covers_drafts_and_pending_approval(self):
        self.queue.cancel_for_qr(self.tx, 2)
        sql = self.tx.execute.call_args.args[0]
        self.assertIn("'DRAFT', 'PENDING_APPROVAL', 'QUEUED', 'FAILED'", sql)

    def test_bulk_batch_filter_is_parameterized_and_preserves_admin_scope(self):
        self.tx.all.return_value = []
        with patch("meal_management.queries.require_actor", return_value=self.actor) as require:
            QueryService(self.db).email_status(self.context, delivery_scope="bulk", bulk_batch_id=19)
        sql, params = self.tx.all.call_args.args
        self.assertIn("q.bulk_batch_id = %s", sql)
        self.assertEqual(params, [0, 19, 51])
        require.assert_called_once_with(self.tx, self.context, {"ADMIN"})
        with self.assertRaisesRegex(DomainError, "INVALID_EMAIL_BATCH_ID"):
            QueryService(self.db).email_status(self.context, bulk_batch_id=True)

    def test_resend_reuses_existing_single_draft_or_queued_email(self):
        qr = QrService(self.db, self.vault, self.renderer)
        qr._lock_employee = Mock(return_value=self.employee)
        qr._active_employee_qr = Mock(return_value=self.credential)
        for status in ("DRAFT", "QUEUED"):
            self.tx.one.return_value = {**self.row, "status": status}
            with patch("meal_management.qr.require_actor", return_value=self.actor):
                self.assertEqual(qr.resend_employee(self.context, 1), 3)
        self.tx.insert.assert_not_called()
        self.vault.decrypt.assert_not_called()

    def test_resend_refuses_uncertain_prior_claim(self):
        qr = QrService(self.db, self.vault, self.renderer)
        qr._lock_employee = Mock(return_value=self.employee)
        qr._active_employee_qr = Mock(return_value=self.credential)
        for code in ("EMAIL_DELIVERY_CLAIMED_private", "EMAIL_DELIVERY_NEEDS_REVIEW", None):
            self.tx.one.return_value = {**self.row, "status": "FAILED", "last_error": code}
            with patch("meal_management.qr.require_actor", return_value=self.actor):
                with self.assertRaisesRegex(DomainError, "EMAIL_DELIVERY_NEEDS_REVIEW"):
                    qr.resend_employee(self.context, 1)
        self.tx.insert.assert_not_called()


class BulkRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.tx = Mock()
        self.tx.now.return_value = datetime(2026, 9, 10, 12)
        self.tx.one.return_value = None
        self.tx.insert.return_value = 8
        self.db = TransactionDatabase(self.tx)
        self.actor = Actor(7, frozenset({"ADMIN"}))
        self.context = ServerContext(generate_token())
        self.service = EmployeeService(self.db, Mock())
        self.service._register = Mock(side_effect=[Registration(11, 12, 13), Registration(21, 22, 23)])
        self.identifier = uuid4()
        self.employees = [
            {"employee_code": "E1", "full_name": "First Employee", "email": "first@example.test", "company_name": "Example Company", "phone": "+1 202 555 0101", "department_id": 1},
            {"employee_code": "E2", "full_name": "Second Employee", "email": "second@example.test", "company_name": "Example Company", "phone": "+1 202 555 0102", "department_id": 2},
        ]
        self.actor_patch = patch("meal_management.employees.require_actor", return_value=self.actor)
        self.actor_patch.start()
        self.addCleanup(self.actor_patch.stop)

    def register(self):
        return self.service.register_bulk(self.context, self.identifier, self.employees)

    def replay(self):
        self.tx.one.return_value = {"id": 8, "request_hash": payload_digest({"employees": self.employees})}
        self.tx.all.return_value = [{"employee_id": 11, "qr_id": 12, "email_id": 13}, {"employee_id": 21, "qr_id": 22, "email_id": 23}]

    def test_bulk_registration_commits_one_batch_and_reuses_single_transaction_helper(self):
        result = self.register()
        self.assertEqual(result, {"batch_id": 8, "employees": [{"employee_id": 11, "qr_id": 12, "email_id": 13}, {"employee_id": 21, "qr_id": 22, "email_id": 23}], "replayed": False})
        self.assertTrue(self.db.committed)
        self.assertEqual(self.service._register.call_count, 2)
        for call in self.service._register.call_args_list:
            self.assertEqual(call.args, (self.tx, self.actor))
            self.assertEqual(call.kwargs["bulk_batch_id"], 8)
        self.assertEqual(self.tx.insert.call_args.args[1][:2], (self.identifier.bytes, 7))

    def test_retry_returns_original_ids_without_new_employee_qr_or_email(self):
        self.replay()
        result = self.register()
        self.assertTrue(result["replayed"])
        self.assertEqual(result["employees"][0]["email_id"], 13)
        self.service._register.assert_not_called()
        self.tx.insert.assert_not_called()

    def test_retry_requires_identical_canonical_payload(self):
        self.replay()
        self.employees[0]["full_name"] = "Changed Name"
        with self.assertRaisesRegex(DomainError, "IDEMPOTENCY_KEY_REUSED"):
            self.register()
        self.service._register.assert_not_called()

    def test_request_binding_uses_authenticated_actor(self):
        self.register()
        self.assertEqual(self.tx.one.call_args.args[1], (7, self.identifier.bytes))
        self.assertIn("created_by_staff_id = %s AND request_id = %s", self.tx.one.call_args.args[0])

    def test_concurrent_unique_collision_recovers_committed_batch(self):
        collision = RuntimeError("Duplicate")
        collision.errno = 1062
        self.tx.insert.side_effect = collision
        self.replay()
        batch = self.tx.one.return_value
        self.tx.one.side_effect = [None, batch]
        self.assertTrue(self.register()["replayed"])
        self.service._register.assert_not_called()

    def test_one_failed_employee_rolls_back_the_entire_bulk_operation(self):
        self.service._register.side_effect = [Registration(11, 12, 13), DomainError("EMPLOYEE_CODE_EXISTS")]
        with self.assertRaisesRegex(DomainError, "EMPLOYEE_CODE_EXISTS"):
            self.register()
        self.assertFalse(self.db.committed)
        self.assertTrue(self.db.rolled_back)

    def test_commit_failure_does_not_return_a_completed_batch(self):
        self.db.commit_error = RuntimeError("COMMIT_FAILED")
        with self.assertRaisesRegex(RuntimeError, "COMMIT_FAILED"):
            self.register()
        self.assertTrue(self.db.rolled_back)

    def test_bad_late_row_is_rejected_before_any_database_access(self):
        self.employees[1]["department_id"] = True
        with self.assertRaisesRegex(DomainError, "INVALID_DEPARTMENT_ID"):
            self.register()
        self.tx.one.assert_not_called()
        self.tx.insert.assert_not_called()

    def test_bulk_count_fields_and_duplicate_codes_are_validated(self):
        for rows in ([], [self.employees[0]] * 101, [{**self.employees[0], "role": "ADMIN"}]):
            with self.assertRaisesRegex(DomainError, "INVALID_EMPLOYEE_BATCH"):
                self.service.register_bulk(self.context, self.identifier, rows)
        duplicate = {**self.employees[0], "employee_code": "e1"}
        with self.assertRaisesRegex(DomainError, "EMPLOYEE_CODE_EXISTS"):
            self.service.register_bulk(self.context, self.identifier, [self.employees[0], duplicate])
        self.tx.one.assert_not_called()

    def test_partial_replay_records_never_report_a_successful_batch(self):
        self.replay()
        self.tx.all.return_value = self.tx.all.return_value[:1]
        with self.assertRaisesRegex(DomainError, "EMPLOYEE_BATCH_UNCONFIRMED"):
            self.register()

    def test_bulk_requires_admin_and_nonzero_request_uuid(self):
        with patch("meal_management.employees.require_actor", side_effect=DomainError("ROLE_REQUIRED")):
            with self.assertRaisesRegex(DomainError, "ROLE_REQUIRED"):
                self.register()
        for identifier in (None, "bad", "00000000-0000-0000-0000-000000000000"):
            with self.assertRaisesRegex(DomainError, "INVALID_REQUEST_ID"):
                self.service.register_bulk(self.context, identifier, self.employees)
        self.tx.one.assert_not_called()


if __name__ == "__main__":
    unittest.main()
