import unittest
from unittest.mock import MagicMock, Mock, patch

from meal_management.catalog import CatalogService
from meal_management.errors import DomainError
from meal_management.models import Actor, ServerContext
from meal_management.security import generate_token


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.tx = Mock()
        self.db = MagicMock()
        self.db.transaction.return_value.__enter__.return_value = self.tx
        self.service = CatalogService(self.db)
        self.context = ServerContext(generate_token())
        self.auth_patch = patch(
            "meal_management.catalog.require_actor",
            return_value=Actor(42, frozenset({"ADMIN"})),
        )
        self.audit_patch = patch("meal_management.catalog.audit")
        self.require_actor = self.auth_patch.start()
        self.audit = self.audit_patch.start()
        self.addCleanup(self.auth_patch.stop)
        self.addCleanup(self.audit_patch.stop)

    def test_location_creation_parameterizes_input_and_audits_authenticated_actor(self):
        name = "North'); SELECT 1"
        self.tx.insert.return_value = 7
        result = self.service.create_location(self.context, "NORTH", name)
        self.assertEqual(result, 7)
        sql, params = self.tx.insert.call_args.args
        self.assertNotIn(name, sql)
        self.assertEqual(params, ("NORTH", name, True))
        self.require_actor.assert_called_once_with(self.tx, self.context, {"ADMIN"})
        self.audit.assert_called_once_with(
            self.tx, 42, "LOCATION_CREATED", "locations", 7,
            after={"code": "NORTH", "name": name, "is_active": True},
        )

    def test_scanner_requires_active_existing_location(self):
        for location in (None, {"id": 1, "is_active": False}):
            with self.subTest(location=location):
                self.tx.one.return_value = location
                with self.assertRaises(DomainError) as caught:
                    self.service.create_scanner(self.context, "SCAN-A", "Counter A", 1)
                self.assertEqual(caught.exception.code, "LOCATION_UNAVAILABLE")
        self.tx.insert.assert_not_called()
        self.audit.assert_not_called()

    def test_scanner_creation_locks_and_records_its_location(self):
        self.tx.one.return_value = {"id": 1, "is_active": True}
        self.tx.insert.return_value = 8
        self.assertEqual(
            self.service.create_scanner(self.context, "SCAN-A", "Counter A", 1), 8
        )
        self.assertIn("FOR SHARE", self.tx.one.call_args.args[0])
        self.assertEqual(self.tx.insert.call_args.args[1], ("SCAN-A", "Counter A", 1, True))

    def test_meal_type_creation_returns_only_after_commit(self):
        self.tx.insert.return_value = 9
        self.db.transaction.return_value.__exit__.side_effect = RuntimeError("commit failed")
        with self.assertRaisesRegex(RuntimeError, "commit failed"):
            self.service.create_meal_type(self.context, "LUNCH", "Lunch")

    def test_creation_duplicate_is_reported_as_domain_error(self):
        error = RuntimeError("duplicate")
        error.errno = 1062
        self.tx.insert.side_effect = error
        with self.assertRaises(DomainError) as caught:
            self.service.create_meal_type(self.context, "LUNCH", "Lunch")
        self.assertEqual(caught.exception.code, "MEAL_TYPE_CODE_EXISTS")
        self.audit.assert_not_called()

    def test_status_change_preserves_record_and_audits_previous_value(self):
        self.tx.one.return_value = {"id": 1, "is_active": True}
        self.service.set_location_active(self.context, 1, False)
        self.assertIn("FOR UPDATE", self.tx.one.call_args.args[0])
        self.tx.execute.assert_called_once_with(
            "UPDATE locations SET is_active = %s WHERE id = %s", (False, 1)
        )
        self.audit.assert_called_once_with(
            self.tx, 42, "LOCATION_STATUS_CHANGED", "locations", 1,
            before={"is_active": True}, after={"is_active": False},
        )

    def test_no_change_does_not_rewrite_or_add_audit_event(self):
        self.tx.one.return_value = {"id": 1, "is_active": False}
        self.service.set_scanner_active(self.context, 1, False)
        self.tx.execute.assert_not_called()
        self.audit.assert_not_called()

    def test_waiter_cannot_modify_catalog(self):
        self.require_actor.side_effect = DomainError("ROLE_REQUIRED")
        with self.assertRaises(DomainError):
            self.service.create_location(self.context, "NORTH", "North")
        self.tx.insert.assert_not_called()
        self.audit.assert_not_called()

    def test_invalid_status_and_identifier_are_rejected_before_database_access(self):
        for identifier, active in ((0, True), (True, True), (1, 1), (1, "false")):
            with self.subTest(identifier=identifier, active=active):
                with self.assertRaises(DomainError):
                    self.service.set_meal_type_active(self.context, identifier, active)
        self.db.transaction.assert_not_called()

    def test_unrecognized_catalog_cannot_be_used_as_sql_identifier(self):
        with self.assertRaises(DomainError) as caught:
            self.service._set_active(self.context, "locations; SELECT 1", 1, True)
        self.assertEqual(caught.exception.code, "INVALID_CATALOG")
        self.db.transaction.assert_not_called()


if __name__ == "__main__":
    unittest.main()
