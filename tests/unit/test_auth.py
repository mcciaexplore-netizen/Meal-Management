import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

from meal_management.auth import require_actor, require_scan_actor
from meal_management.errors import DomainError
from meal_management.models import Actor, ScanAppContext, ServerContext
from meal_management.security import generate_token


class AuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 9, 10, 0)
        self.transaction = Mock()
        self.transaction.now.return_value = self.now
        self.transaction.one.return_value = {
            "id": 1,
            "staff_id": 42,
            "expires_at": self.now + timedelta(hours=1),
            "revoked_at": None,
            "last_seen_at": self.now,
            "is_active": True,
        }
        self.transaction.all.return_value = [{"role_code": "WAITER"}]
        self.context = ServerContext(generate_token())

    def test_identity_comes_from_database_session(self):
        actor = require_actor(self.transaction, self.context, {"WAITER"})
        self.assertEqual(actor.staff_id, 42)
        self.assertEqual(actor.roles, frozenset({"WAITER"}))
        self.transaction.execute.assert_called_once_with(
            "UPDATE staff_sessions SET last_seen_at = %s WHERE id = %s", (self.now, 1)
        )

    def test_claimed_staff_identity_without_session_is_rejected(self):
        with self.assertRaises(DomainError) as caught:
            require_actor(self.transaction, {"staff_id": 42, "roles": ["ADMIN"]}, {"ADMIN"})
        self.assertEqual(caught.exception.code, "AUTHENTICATION_REQUIRED")
        self.transaction.one.assert_not_called()

    def test_expired_revoked_and_disabled_sessions_are_rejected(self):
        for changed in (
            {"expires_at": self.now},
            {"revoked_at": self.now},
            {"is_active": False},
            {"last_seen_at": self.now - timedelta(minutes=30)},
        ):
            with self.subTest(changed=changed):
                original = self.transaction.one.return_value.copy()
                self.transaction.one.return_value.update(changed)
                with self.assertRaises(DomainError):
                    require_actor(self.transaction, self.context, {"WAITER"})
                self.transaction.one.return_value = original

    def test_waiter_cannot_authorize_as_admin(self):
        with self.assertRaises(DomainError) as caught:
            require_actor(self.transaction, self.context, {"ADMIN"})
        self.assertEqual(caught.exception.code, "ROLE_REQUIRED")

    def test_custom_idle_timeout_is_enforced(self):
        self.transaction.one.return_value["last_seen_at"] = self.now - timedelta(minutes=2)
        with self.assertRaises(DomainError) as caught:
            require_actor(self.transaction, ServerContext(generate_token(), 60), {"WAITER"})
        self.assertEqual(caught.exception.code, "AUTHENTICATION_REQUIRED")
        self.transaction.execute.assert_not_called()

    def test_invalid_idle_timeout_is_rejected(self):
        for value in (True, 0, 59, 604801, "1800"):
            with self.subTest(value=value):
                with self.assertRaises(DomainError) as caught:
                    require_actor(self.transaction, ServerContext(generate_token(), value), {"WAITER"})
                self.assertEqual(caught.exception.code, "INVALID_SESSION_IDLE_TIMEOUT")
        self.transaction.one.assert_not_called()

    def test_scanner_context_cannot_authorize_human_staff_operations(self):
        with self.assertRaisesRegex(DomainError, "AUTHENTICATION_REQUIRED"):
            require_actor(self.transaction, ScanAppContext(42), {"ADMIN"})
        self.transaction.one.assert_not_called()

    def test_scanner_account_cannot_use_a_staff_session(self):
        self.transaction.one.return_value["is_scanner"] = True
        with self.assertRaisesRegex(DomainError, "AUTHENTICATION_REQUIRED"):
            require_actor(self.transaction, self.context, {"WAITER"})
        self.transaction.all.assert_not_called()
        self.transaction.execute.assert_not_called()


class SharedScannerAuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.transaction = Mock()
        self.transaction.one.return_value = {
            "staff_id": 42, "is_enabled": True, "is_scanner": True, "is_active": True,
        }
        self.transaction.all.return_value = [{"role_code": "WAITER"}]
        self.context = ScanAppContext(42)

    def test_scanner_uses_only_server_configured_identity_without_a_session(self):
        actor = require_scan_actor(self.transaction, self.context)
        self.assertEqual(actor, Actor(42, frozenset({"WAITER"})))
        sql, values = self.transaction.one.call_args.args
        self.assertIn("p.id = 1 AND p.staff_id = %s FOR SHARE", sql)
        self.assertEqual(values, (42,))
        self.assertNotIn("staff_sessions", sql)
        self.transaction.insert.assert_not_called()
        self.transaction.execute.assert_not_called()

    def test_missing_disabled_human_or_changed_profile_is_rejected(self):
        profile = self.transaction.one.return_value
        for row in (
            None, {**profile, "staff_id": 99}, {**profile, "is_enabled": False},
            {**profile, "is_scanner": False}, {**profile, "is_active": False},
        ):
            with self.subTest(row=row):
                self.transaction.one.return_value = row
                with self.assertRaisesRegex(DomainError, "SCAN_APP_UNAVAILABLE"):
                    require_scan_actor(self.transaction, self.context)
        self.transaction.all.assert_not_called()

    def test_missing_or_elevated_scanner_roles_are_rejected(self):
        for roles in ([], ["ADMIN"], ["WAITER", "ADMIN"], ["WAITER", "AUDITOR"]):
            with self.subTest(roles=roles):
                self.transaction.all.return_value = [{"role_code": role} for role in roles]
                with self.assertRaisesRegex(DomainError, "SCAN_APP_UNAVAILABLE"):
                    require_scan_actor(self.transaction, self.context)
        self.transaction.execute.assert_not_called()

    def test_invalid_server_scanner_identity_is_rejected_before_querying(self):
        for identifier in (True, False, 0, -1, 2**64, "42", None):
            with self.subTest(identifier=identifier):
                with self.assertRaisesRegex(DomainError, "SCAN_APP_UNAVAILABLE"):
                    require_scan_actor(self.transaction, ScanAppContext(identifier))
        self.transaction.one.assert_not_called()

    def test_human_context_keeps_existing_staff_authentication(self):
        context = ServerContext(generate_token())
        actor = Actor(7, frozenset({"ADMIN"}))
        with patch("meal_management.auth.require_actor", return_value=actor) as authenticate:
            self.assertEqual(require_scan_actor(self.transaction, context), actor)
        authenticate.assert_called_once_with(self.transaction, context, {"ADMIN", "WAITER"})

    def test_submitted_identity_dictionary_is_not_a_shared_scanner_context(self):
        with self.assertRaisesRegex(DomainError, "AUTHENTICATION_REQUIRED"):
            require_scan_actor(self.transaction, {"staff_id": 42, "is_scanner": True})
        self.transaction.one.assert_not_called()


if __name__ == "__main__":
    unittest.main()
