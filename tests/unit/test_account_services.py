import json
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from meal_management.accounts import StaffService
from meal_management.email_queue import EmailPreview, EmailQueueService
from meal_management.employees import EmployeeService
from meal_management.errors import DomainError
from meal_management.models import Actor, ServerContext
from meal_management.qr import IssuedQr, QrService
from meal_management.security import generate_token, token_digest


class TransactionDatabase:
    def __init__(self, tx, commit_error=None):
        self.tx = tx
        self.commit_error = commit_error
        self.committed = False
        self.rolled_back = False

    @contextmanager
    def transaction(self):
        try:
            yield self.tx
            if self.commit_error is not None:
                raise self.commit_error
            self.committed = True
        except BaseException:
            self.rolled_back = True
            raise


class AccountServiceTests(unittest.TestCase):
    def setUp(self):
        self.tx = Mock()
        self.tx.now.return_value = datetime(2026, 9, 9, 12, 0)
        self.db = TransactionDatabase(self.tx)
        self.context = ServerContext(generate_token())
        self.actor = Actor(7, frozenset({"ADMIN"}))

    def test_staff_roles_reject_unrecognized_values_before_database_access(self):
        service = StaffService(self.db)
        with self.assertRaises(DomainError) as error:
            service.create_staff(
                self.context,
                "Test Waiter",
                "waiter@example.com",
                generate_token(),
                ["UNRECOGNIZED"],
            )
        self.assertEqual(error.exception.code, "INVALID_ROLES")
        self.assertEqual(self.tx.method_calls, [])
        self.assertFalse(self.db.committed)

    def test_invalid_password_never_creates_session(self):
        self.tx.one.return_value = {"id": 4, "password_hash": "encoded", "is_active": True}
        with patch("meal_management.accounts.verify_password", return_value=False):
            with self.assertRaises(DomainError) as error:
                StaffService(self.db).authenticate("waiter@example.com", generate_token())
        self.assertEqual(error.exception.code, "INVALID_CREDENTIALS")
        self.tx.insert.assert_not_called()
        self.assertTrue(self.db.rolled_back)

    def test_authentication_returns_no_context_when_commit_fails(self):
        self.tx.one.return_value = {"id": 4, "password_hash": "encoded", "is_active": True}
        self.db.commit_error = RuntimeError("COMMIT_FAILED")
        with patch("meal_management.accounts.verify_password", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "COMMIT_FAILED"):
                StaffService(self.db).authenticate("waiter@example.com", generate_token())
        self.assertFalse(self.db.committed)
        self.assertTrue(self.db.rolled_back)
        session_params = self.tx.insert.call_args.args[1]
        self.assertIsInstance(session_params[1], bytes)
        self.assertEqual(len(session_params[1]), 32)

    def test_last_active_admin_cannot_lose_admin_role(self):
        self.tx.one.side_effect = [
            {"name": "bootstrap"},
            {"id": 7, "is_active": True},
            {"total": 1},
        ]
        self.tx.all.return_value = [{"role_code": "ADMIN"}]
        with patch("meal_management.accounts.require_actor", return_value=self.actor):
            with self.assertRaises(DomainError) as error:
                StaffService(self.db).set_roles(self.context, 7, ["WAITER"])
        self.assertEqual(error.exception.code, "LAST_ACTIVE_ADMIN")
        self.tx.execute.assert_not_called()
        self.assertTrue(self.db.rolled_back)

    def test_role_changes_require_authorized_server_context(self):
        self.tx.one.return_value = {"name": "bootstrap"}
        with patch(
            "meal_management.accounts.require_actor",
            side_effect=DomainError("ROLE_REQUIRED"),
        ):
            with self.assertRaises(DomainError) as error:
                StaffService(self.db).set_roles(self.context, 9, ["ADMIN"])
        self.assertEqual(error.exception.code, "ROLE_REQUIRED")
        self.tx.execute.assert_not_called()
        self.tx.insert.assert_not_called()
        self.assertTrue(self.db.rolled_back)

    def test_current_staff_identity_is_loaded_from_authenticated_actor(self):
        self.tx.one.return_value = {"staff_id": 7, "display_name": "Test Admin", "email": "admin@example.com"}
        with patch("meal_management.accounts.require_actor", return_value=self.actor):
            result = StaffService(self.db).current(self.context)
        self.assertEqual(result["staff_id"], 7)
        self.assertEqual(result["roles"], ["ADMIN"])
        self.assertEqual(self.tx.one.call_args.args[1], (7,))
        self.assertNotIn("password", self.tx.one.call_args.args[0])
        self.assertTrue(self.db.committed)

    def test_logout_revokes_token_without_requiring_unexpired_session(self):
        with patch("meal_management.accounts.require_actor") as require:
            StaffService(self.db).logout(self.context)
        require.assert_not_called()
        self.assertEqual(self.tx.execute.call_args.args[1][1], token_digest(self.context.session_token))
        self.assertIn("revoked_at IS NULL", self.tx.execute.call_args.args[0])
        self.assertTrue(self.db.committed)

    def test_logout_without_valid_token_is_idempotent(self):
        service = StaffService(self.db)
        for context in (None, {}, ServerContext("invalid")):
            service.logout(context)
        self.tx.execute.assert_not_called()

    def test_authenticated_session_initializes_last_seen(self):
        self.tx.one.return_value = {"id": 4, "password_hash": "encoded", "is_active": True}
        with patch("meal_management.accounts.verify_password", return_value=True):
            StaffService(self.db).authenticate("waiter@example.com", generate_token())
        sql, params = self.tx.insert.call_args.args
        self.assertIn("last_seen_at", sql)
        self.assertEqual(params[2], params[4])

    def test_scanner_login_never_issues_a_session_even_when_verification_succeeds(self):
        self.tx.one.return_value = {
            "id": 4, "password_hash": None, "is_active": True, "is_scanner": True,
        }
        with (
            patch("meal_management.accounts._dummy_password_hash", return_value="dummy-encoded"),
            patch("meal_management.accounts.verify_password", return_value=True) as verify,
            patch("meal_management.accounts.generate_token") as token,
        ):
            with self.assertRaisesRegex(DomainError, "INVALID_CREDENTIALS"):
                StaffService(self.db).authenticate("meal-scanner@system.invalid", "fictional-attempt")
        verify.assert_called_once_with("fictional-attempt", "dummy-encoded")
        token.assert_not_called()
        self.tx.insert.assert_not_called()
        self.assertTrue(self.db.rolled_back)

    def test_first_human_admin_can_bootstrap_after_scanner_setup(self):
        self.tx.one.side_effect = [{"name": "bootstrap"}, None, {"code": "ADMIN"}]
        self.tx.insert.return_value = 7
        with patch("meal_management.accounts.hash_password", return_value="secure-encoded"):
            identifier = StaffService(self.db).bootstrap_admin(
                "First Admin", "admin@example.test", "fictional-entered-password"
            )
        self.assertEqual(identifier, 7)
        self.assertIn("WHERE is_scanner = 0 LIMIT 1", self.tx.one.call_args_list[1].args[0])
        self.assertEqual(self.tx.insert.call_args_list[0].args[1][2], "secure-encoded")
        self.assertTrue(self.db.committed)

    def test_existing_human_account_still_prevents_bootstrap(self):
        self.tx.one.side_effect = [{"name": "bootstrap"}, {"id": 7}]
        with patch("meal_management.accounts.hash_password", return_value="secure-encoded"):
            with self.assertRaisesRegex(DomainError, "ADMIN_ALREADY_INITIALIZED"):
                StaffService(self.db).bootstrap_admin(
                    "Another Admin", "another@example.test", "fictional-entered-password"
                )
        self.tx.insert.assert_not_called()

    def test_scanner_roles_cannot_be_changed_through_staff_administration(self):
        self.tx.one.side_effect = [
            {"name": "bootstrap"}, {"id": 4, "is_active": True, "is_scanner": True},
        ]
        with patch("meal_management.accounts.require_actor", return_value=self.actor):
            with self.assertRaisesRegex(DomainError, "SCANNER_ROLES_IMMUTABLE"):
                StaffService(self.db).set_roles(self.context, 4, ["ADMIN"])
        self.tx.all.assert_not_called()
        self.tx.execute.assert_not_called()


class EmployeeQrServiceTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 9, 12, 0)
        self.tx = Mock()
        self.tx.now.return_value = self.now
        self.db = TransactionDatabase(self.tx)
        self.vault = Mock()
        self.vault.encrypt.return_value = b"encrypted-value"
        self.renderer = Mock()
        self.renderer.render.return_value = "<svg></svg>"
        self.qr = QrService(self.db, self.vault, self.renderer)
        self.employees = EmployeeService(self.db, self.qr)
        self.context = ServerContext(generate_token())
        self.actor = Actor(7, frozenset({"ADMIN"}))
        self.employee = {
            "id": 41,
            "full_name": "Asha Rao",
            "email": "asha@example.com",
            "is_active": True,
        }
        self.token = generate_token()
        self.credential = {
            "id": 72,
            "kind": "EMPLOYEE",
            "employee_id": 41,
            "token_hash": token_digest(self.token),
            "token_ciphertext": b"encrypted-token",
            "expires_at": None,
            "revoked_at": None,
        }

    def _patch_actor(self, module):
        return patch("meal_management." + module + ".require_actor", return_value=self.actor)

    def test_registration_rolls_back_employee_and_qr_when_rendering_fails(self):
        self.tx.one.return_value = {"id": 3, "is_active": True}
        self.tx.insert.side_effect = [41, 72]
        self.renderer.render.side_effect = RuntimeError("RENDER_FAILED")
        with self._patch_actor("employees"), patch("meal_management.qr.audit"):
            with self.assertRaisesRegex(RuntimeError, "RENDER_FAILED"):
                self.employees.register(self.context, "EMP-1", "Asha Rao", "asha@example.com", 3)
        self.assertTrue(self.db.rolled_back)
        self.assertFalse(self.db.committed)
        self.assertEqual(self.tx.insert.call_count, 2)

    def test_registration_commits_employee_qr_and_encrypted_email_together(self):
        self.tx.one.return_value = {"id": 3, "is_active": True}
        self.tx.insert.side_effect = [41, 72, 93]
        with (
            self._patch_actor("employees"),
            patch("meal_management.qr.audit"),
            patch("meal_management.employees.audit"),
        ):
            registration = self.employees.register(
                self.context, "EMP-1", "Asha Rao", "ASHA@example.com", 3
            )
        self.assertTrue(self.db.committed)
        self.assertEqual((registration.employee_id, registration.qr_id, registration.email_id), (41, 72, 93))
        qr_params = self.tx.insert.call_args_list[1].args[1]
        email_params = self.tx.insert.call_args_list[2].args[1]
        token = self.renderer.render.call_args.args[0]
        self.assertEqual(qr_params[2], token_digest(token))
        self.assertEqual(qr_params[3], b"encrypted-value")
        self.assertEqual(email_params[1], "asha@example.com")
        self.assertEqual(email_params[2], b"encrypted-value")
        self.assertNotIn(token, repr(self.tx.insert.call_args_list))

    def test_registration_stores_normalized_company_and_phone_contact_details(self):
        self.tx.one.return_value = {"id": 3, "is_active": True}
        self.tx.insert.side_effect = [41, 72, 93]
        with (
            self._patch_actor("employees"),
            patch("meal_management.qr.audit"),
            patch("meal_management.employees.audit"),
        ):
            self.employees.register(
                self.context,
                "EMP-1",
                "Asha Rao",
                "asha@example.com",
                3,
                company_name="  Example Company  ",
                phone="  +1 202 555 0107  ",
            )
        sql, params = self.tx.insert.call_args_list[0].args
        self.assertIn("company_name, phone", sql)
        self.assertEqual(params[3:5], ("Example Company", "+1 202 555 0107"))

    def test_registration_rejects_invalid_employee_phone_before_database_access(self):
        with self.assertRaisesRegex(DomainError, "INVALID_EMPLOYEE_PHONE"):
            self.employees.register(
                self.context,
                "EMP-1",
                "Asha Rao",
                "asha@example.com",
                3,
                company_name="Example Company",
                phone="invalid phone",
            )
        self.tx.one.assert_not_called()
        self.tx.insert.assert_not_called()

    def test_registration_returns_no_result_when_commit_fails(self):
        self.tx.one.return_value = {"id": 3, "is_active": True}
        self.tx.insert.side_effect = [41, 72, 93]
        self.db.commit_error = RuntimeError("COMMIT_FAILED")
        with (
            self._patch_actor("employees"),
            patch("meal_management.qr.audit"),
            patch("meal_management.employees.audit"),
        ):
            with self.assertRaisesRegex(RuntimeError, "COMMIT_FAILED"):
                self.employees.register(
                    self.context, "EMP-1", "Asha Rao", "asha@example.com", 3
                )
        self.assertFalse(self.db.committed)
        self.assertTrue(self.db.rolled_back)

    def test_resend_uses_existing_token_without_issuing_another_credential(self):
        self.tx.one.side_effect = [self.employee, self.credential, None]
        self.tx.insert.return_value = 93
        self.vault.decrypt.return_value = self.token
        with self._patch_actor("qr"), patch("meal_management.qr.audit"):
            email_id = self.qr.resend_employee(self.context, 41)
        self.assertEqual(email_id, 93)
        self.renderer.render.assert_called_once_with(self.token)
        self.assertEqual(self.tx.insert.call_count, 1)
        self.assertIn("INSERT INTO email_queue", self.tx.insert.call_args.args[0])
        self.assertTrue(self.db.committed)

    def test_issue_employee_does_not_replace_existing_credential(self):
        self.tx.one.side_effect = [self.employee, {"id": 72}]
        with self._patch_actor("qr"):
            with self.assertRaises(DomainError) as caught:
                self.qr.issue_employee(self.context, 41)
        self.assertEqual(caught.exception.code, "EMPLOYEE_QR_ALREADY_EXISTS")
        self.tx.execute.assert_not_called()
        self.tx.insert.assert_not_called()
        self.assertTrue(self.db.rolled_back)

    def test_issue_employee_locks_owner_and_atomically_queues_new_credential(self):
        self.tx.one.side_effect = [self.employee, None]
        self.tx.insert.side_effect = [72, 93]
        with self._patch_actor("qr"), patch("meal_management.qr.audit"):
            result = self.qr.issue_employee(self.context, 41)
        self.assertEqual(result.qr_id, 72)
        self.assertIn("FOR UPDATE", self.tx.one.call_args_list[0].args[0])
        self.assertIn("FOR UPDATE", self.tx.one.call_args_list[1].args[0])
        self.assertEqual(self.tx.insert.call_count, 2)
        self.assertIn("INSERT INTO email_queue", self.tx.insert.call_args_list[1].args[0])
        self.assertTrue(self.db.committed)

    def test_expired_qr_cannot_be_resent(self):
        self.credential["expires_at"] = self.now - timedelta(seconds=1)
        self.tx.one.side_effect = [self.employee, self.credential]
        with self._patch_actor("qr"):
            with self.assertRaises(DomainError) as error:
                self.qr.resend_employee(self.context, 41)
        self.assertEqual(error.exception.code, "QR_EXPIRED")
        self.vault.decrypt.assert_not_called()
        self.tx.insert.assert_not_called()

    def test_revoked_qr_cannot_be_exported(self):
        self.credential["revoked_at"] = self.now - timedelta(seconds=1)
        self.tx.one.side_effect = [
            {"employee_id": 41},
            self.employee,
            self.credential,
        ]
        with self._patch_actor("qr"):
            with self.assertRaises(DomainError) as error:
                self.qr.export_svg(self.context, 72)
        self.assertEqual(error.exception.code, "QR_REVOKED")
        self.renderer.render.assert_not_called()
        self.vault.decrypt.assert_not_called()

    def test_replacement_failure_rolls_back_revocation_and_queue_cancellation(self):
        self.tx.one.side_effect = [self.employee, {"id": 72, "revoked_at": None}]
        self.tx.insert.return_value = 73
        self.renderer.render.side_effect = RuntimeError("RENDER_FAILED")
        with self._patch_actor("qr"), patch("meal_management.qr.audit"):
            with self.assertRaisesRegex(RuntimeError, "RENDER_FAILED"):
                self.qr.replace_employee(self.context, 41)
        self.assertTrue(self.db.rolled_back)
        self.assertFalse(self.db.committed)
        self.assertEqual(self.tx.execute.call_count, 2)

    def test_new_expiry_requires_timezone_and_future_time(self):
        with self._patch_actor("qr"):
            for expiry, code in (
                (self.now, "TIMEZONE_REQUIRED"),
                (self.now.replace(tzinfo=timezone.utc), "INVALID_QR_EXPIRY"),
            ):
                with self.subTest(code=code):
                    with self.assertRaises(DomainError) as error:
                        self.qr.issue_master(self.context, expires_at=expiry)
                    self.assertEqual(error.exception.code, code)
        self.tx.insert.assert_not_called()

    def test_master_request_stores_normalized_group_details_and_meal_limit_atomically(self):
        self.tx.insert.return_value = 72
        with self._patch_actor("qr"), patch("meal_management.qr.audit"):
            issued = self.qr.issue_master(
                self.context, company_name=" Example Company ", contact_name=" Ravi Patel ",
                email=" RAVI@EXAMPLE.TEST ", phone=" +91 98765 43210 ", meal_limit=5,
            )
        self.assertEqual(issued.qr_id, 72)
        allocation = next(call for call in self.tx.execute.call_args_list if call.args[0].startswith("INSERT INTO master_qr_allocations"))
        self.assertEqual(allocation.args[1], (72, "Example Company", "Ravi Patel", "ravi@example.test", "+91 98765 43210", 5, 7))
        self.assertTrue(self.db.committed)

    def test_master_request_rejects_incomplete_details_or_invalid_allowance_before_writing(self):
        for values in (
            {"company_name": "", "contact_name": "Ravi", "email": "ravi@example.test", "phone": "+919876543210", "meal_limit": 5},
            {"company_name": "Example", "contact_name": "Ravi", "email": "invalid", "phone": "+919876543210", "meal_limit": 5},
            {"company_name": "Example", "contact_name": "Ravi", "email": "ravi@example.test", "phone": "+919876543210", "meal_limit": 0},
        ):
            with self.subTest(values=values):
                with self.assertRaises(DomainError):
                    self.qr.issue_master(self.context, **values)
        self.tx.insert.assert_not_called()

    def test_tampered_encrypted_token_cannot_be_resent(self):
        self.tx.one.side_effect = [self.employee, self.credential, None]
        self.vault.decrypt.return_value = generate_token()
        with self._patch_actor("qr"):
            with self.assertRaises(DomainError) as error:
                self.qr.resend_employee(self.context, 41)
        self.assertEqual(error.exception.code, "QR_TOKEN_MISMATCH")
        self.tx.insert.assert_not_called()

    def test_retrieve_requires_admin_and_returns_existing_verified_token(self):
        self.tx.one.side_effect = [{"employee_id": 41}, self.employee, self.credential]
        self.vault.decrypt.return_value = self.token
        with self._patch_actor("qr") as require, patch("meal_management.qr.audit") as audit:
            issued = self.qr.retrieve(self.context, 72)
        require.assert_called_once_with(self.tx, self.context, {"ADMIN"})
        self.assertEqual(issued.token, self.token)
        self.assertEqual(issued.qr_id, 72)
        self.assertTrue(self.db.committed)
        self.assertEqual(audit.call_args.args[2], "QR_RETRIEVED")

    def test_retrieve_does_not_decrypt_for_unauthorized_actor(self):
        with patch("meal_management.qr.require_actor", side_effect=DomainError("ROLE_REQUIRED")):
            with self.assertRaises(DomainError):
                self.qr.retrieve(self.context, 72)
        self.vault.decrypt.assert_not_called()

    def test_credential_and_preview_representations_hide_bearer_tokens(self):
        issued = IssuedQr(qr_id=72, token=self.token)
        preview = EmailPreview(93, "asha@example.com", "Asha Rao", self.token, self.token)
        self.assertNotIn(self.token, repr(issued))
        self.assertNotIn(self.token, repr(preview))
        self.assertNotIn(self.context.session_token, repr(self.context))

    def test_preview_rejects_payload_for_another_employee(self):
        self.tx.one.side_effect = [
            {"employee_id": 41, "qr_id": 72},
            self.employee,
            self.credential,
            {
                "id": 93,
                "recipient_email": "asha@example.com",
                "payload_ciphertext": b"encrypted-value",
                "status": "QUEUED",
            },
        ]
        self.vault.decrypt.return_value = json.dumps(
            {
                "employee_id": 99,
                "employee_name": "Asha Rao",
                "token": self.token,
                "qr_svg": "<svg></svg>",
            }
        )
        queue = EmailQueueService(self.db, self.vault, self.renderer)
        with self._patch_actor("email_queue"):
            with self.assertRaises(DomainError) as error:
                queue.preview(self.context, 93)
        self.assertEqual(error.exception.code, "INVALID_EMAIL_PAYLOAD")


if __name__ == "__main__":
    unittest.main()
