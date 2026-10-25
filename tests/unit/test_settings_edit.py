import unittest
from datetime import datetime
from unittest.mock import MagicMock, Mock, patch

from meal_management.accounts import StaffService
from meal_management.catalog import CatalogService
from meal_management.errors import DomainError
from meal_management.meals import _insert_void
from meal_management.models import Actor, ServerContext
from meal_management.security import generate_token
from test_api import ApiHarness, ApiTestCase


class DuplicateKey(Exception):
    errno = 1062


class TriggerSignal(Exception):
    errno = 1644


class CatalogEditTests(unittest.TestCase):
    def setUp(self):
        self.tx = Mock()
        self.db = MagicMock()
        self.db.transaction.return_value.__enter__.return_value = self.tx
        self.service = CatalogService(self.db)
        self.context = ServerContext(generate_token())
        for target, value in (("meal_management.catalog.require_actor", Actor(42, frozenset({"ADMIN"}))), ("meal_management.catalog.audit", None)):
            patcher = patch(target, return_value=value)
            started = patcher.start()
            self.addCleanup(patcher.stop)
            if target.endswith("audit"):
                self.audit = started

    def shared(self, scanner=3, location=4, meal_type=5):
        return {"scanner_id": scanner, "location_id": location, "meal_type_id": meal_type}

    def test_edit_updates_code_and_name_with_before_and_after_audit(self):
        self.tx.one.side_effect = [{"id": 8, "code": "C2", "name": "Counter 2"}, self.shared()]
        result = self.service.update_scanner(self.context, 8, " C2-NORTH ", " North counter ")
        self.assertEqual(result, {"id": 8, "code": "C2-NORTH", "name": "North counter"})
        self.tx.execute.assert_called_once_with(
            "UPDATE scanner_devices SET code = %s, name = %s WHERE id = %s", ("C2-NORTH", "North counter", 8),
        )
        self.audit.assert_called_once_with(
            self.tx, 42, "SCANNER_UPDATED", "scanner_devices", 8,
            before={"code": "C2", "name": "Counter 2"}, after={"code": "C2-NORTH", "name": "North counter"},
        )

    def test_used_code_reports_which_kind_of_code_already_exists(self):
        for method, code in (("update_location", "LOCATION_CODE_EXISTS"), ("update_scanner", "SCANNER_CODE_EXISTS"),
                             ("update_meal_type", "MEAL_TYPE_CODE_EXISTS")):
            self.tx.one.side_effect = [{"id": 9, "code": "OLD", "name": "Old"}, self.shared()]
            self.tx.execute.side_effect = DuplicateKey()
            with self.subTest(method=method), self.assertRaisesRegex(DomainError, code):
                getattr(self.service, method)(self.context, 9, "TAKEN", "Old")
        self.audit.assert_not_called()

    def test_shared_scanner_entries_can_be_renamed_but_not_recoded_or_deactivated(self):
        self.tx.one.side_effect = [{"id": 3, "code": "SHARED-SCANNER", "name": "Meal Scanner"}, self.shared()]
        self.service.update_scanner(self.context, 3, "SHARED-SCANNER", "Main counter")
        self.tx.execute.assert_called_once()
        for method, identifier in (("update_scanner", 3), ("update_location", 4), ("update_meal_type", 5)):
            self.tx.one.side_effect = [{"id": identifier, "code": "BUILT-IN", "name": "Built in"}, self.shared()]
            with self.subTest(method=method), self.assertRaisesRegex(DomainError, "SHARED_SCANNER_SETTING_PROTECTED"):
                getattr(self.service, method)(self.context, identifier, "NEW-CODE", "Built in")
        for method, identifier in (("set_scanner_active", 3), ("set_location_active", 4), ("set_meal_type_active", 5)):
            self.tx.one.side_effect = [{"id": identifier, "code": "BUILT-IN", "name": "Built in", "is_active": True}, self.shared()]
            with self.subTest(method=method), self.assertRaisesRegex(DomainError, "SHARED_SCANNER_SETTING_PROTECTED"):
                getattr(self.service, method)(self.context, identifier, False)

    def test_database_guard_signal_is_reported_as_protected(self):
        self.tx.one.side_effect = [{"id": 8, "code": "C8", "name": "Counter", "is_active": True}, self.shared()]
        self.tx.execute.side_effect = TriggerSignal()
        with self.assertRaisesRegex(DomainError, "SHARED_SCANNER_SETTING_PROTECTED"):
            self.service.set_scanner_active(self.context, 8, False)

    def test_department_with_active_employees_requires_confirmation(self):
        row = {"id": 6, "name": "Auditorium", "is_active": True}
        self.tx.one.side_effect = [row, {"total": 12}]
        with self.assertRaisesRegex(DomainError, "DEPARTMENT_HAS_ACTIVE_EMPLOYEES"):
            self.service.set_department_active(self.context, 6, False)
        self.tx.execute.assert_not_called()
        self.tx.one.side_effect = [row, {"total": 12}]
        result = self.service.set_department_active(self.context, 6, False, confirm=True)
        self.assertEqual(result, {"id": 6, "is_active": False, "active_employees": 12})
        self.audit.assert_called_once_with(
            self.tx, 42, "DEPARTMENT_STATUS_CHANGED", "departments", 6,
            before={"is_active": True}, after={"is_active": False, "name": "Auditorium", "active_employees": 12},
        )

    def test_department_without_active_employees_and_reactivation_need_no_confirmation(self):
        self.tx.one.side_effect = [{"id": 6, "name": "Auditorium", "is_active": True}, {"total": 0}]
        self.assertFalse(self.service.set_department_active(self.context, 6, False)["is_active"])
        self.tx.one.side_effect = [{"id": 6, "name": "Auditorium", "is_active": False}, {"total": 3}]
        self.assertTrue(self.service.set_department_active(self.context, 6, True)["is_active"])

    def test_department_rename_conflict_and_validation(self):
        self.tx.one.side_effect = [{"id": 6, "name": "Auditorium"}]
        self.tx.execute.side_effect = DuplicateKey()
        with self.assertRaisesRegex(DomainError, "DEPARTMENT_NAME_EXISTS"):
            self.service.update_department(self.context, 6, "Exhibition Centre")
        for name in ("", "x" * 101):
            with self.assertRaisesRegex(DomainError, "INVALID_DEPARTMENT_NAME"):
                self.service.update_department(self.context, 6, name)
        for value in (0, "6", True):
            with self.assertRaises(DomainError):
                self.service.update_department(self.context, value, "Auditorium")

    def test_unchanged_edits_write_nothing(self):
        self.tx.one.side_effect = [{"id": 8, "code": "C2", "name": "Counter 2"}]
        self.service.update_scanner(self.context, 8, "C2", "Counter 2")
        self.tx.one.side_effect = [{"id": 6, "name": "Auditorium"}]
        self.service.update_department(self.context, 6, "Auditorium")
        self.tx.execute.assert_not_called()
        self.audit.assert_not_called()


class StaffEditTests(unittest.TestCase):
    def setUp(self):
        self.tx = Mock()
        self.tx.now.return_value = datetime(2026, 10, 25, 10)
        self.tx.execute.return_value = 2
        self.db = MagicMock()
        self.db.transaction.return_value.__enter__.return_value = self.tx
        self.service = StaffService(self.db)
        self.context = ServerContext(generate_token())
        patcher = patch("meal_management.accounts.require_actor", return_value=Actor(1, frozenset({"ADMIN"})))
        patcher.start()
        self.addCleanup(patcher.stop)
        audit = patch("meal_management.accounts.audit")
        self.audit = audit.start()
        self.addCleanup(audit.stop)

    def test_email_change_ends_sessions_and_is_audited(self):
        self.tx.one.side_effect = [{"id": 7, "display_name": "Asha", "email": "asha@example.com", "is_scanner": False}, None]
        result = self.service.update(self.context, 7, "Asha Rao", "Asha.Rao@Example.com")
        self.assertEqual(result, {"staff_id": 7, "sessions_ended": 2})
        statements = [call.args[0] for call in self.tx.execute.call_args_list]
        self.assertTrue(statements[0].startswith("UPDATE staff_accounts SET display_name"))
        self.assertTrue(statements[1].startswith("UPDATE staff_sessions SET revoked_at"))
        self.audit.assert_called_once_with(
            self.tx, 1, "STAFF_UPDATED", "staff_accounts", 7,
            before={"display_name": "Asha", "email": "asha@example.com"},
            after={"display_name": "Asha Rao", "email": "asha.rao@example.com", "sessions_ended": 2},
        )

    def test_name_only_change_keeps_sessions(self):
        self.tx.one.side_effect = [{"id": 7, "display_name": "Asha", "email": "asha@example.com", "is_scanner": False}]
        self.assertEqual(self.service.update(self.context, 7, "Asha Rao", "asha@example.com"), {"staff_id": 7, "sessions_ended": 0})
        self.assertEqual(len(self.tx.execute.call_args_list), 1)

    def test_used_email_and_scanner_identity_are_refused(self):
        self.tx.one.side_effect = [{"id": 7, "display_name": "Asha", "email": "asha@example.com", "is_scanner": False}, {"id": 8}]
        with self.assertRaisesRegex(DomainError, "STAFF_EMAIL_EXISTS"):
            self.service.update(self.context, 7, "Asha", "taken@example.com")
        self.tx.one.side_effect = [{"id": 2, "display_name": "Meal Scanner", "email": "meal-scanner@system.invalid", "is_scanner": True}]
        with self.assertRaisesRegex(DomainError, "SCANNER_ACCOUNT_PROTECTED"):
            self.service.update(self.context, 2, "Renamed", "meal-scanner@system.invalid")
        self.tx.execute.assert_not_called()

    def test_scanner_identity_cannot_be_deactivated(self):
        self.tx.one.side_effect = [{"name": "bootstrap"}, {"id": 2, "is_active": True, "is_scanner": True}]
        with self.assertRaisesRegex(DomainError, "SCANNER_ACCOUNT_PROTECTED"):
            self.service.set_active(self.context, 2, False)
        self.tx.execute.assert_not_called()


class MealVoidTests(unittest.TestCase):
    def test_duplicate_void_insert_is_reported_as_already_removed(self):
        tx = Mock()
        tx.insert.side_effect = DuplicateKey()
        with self.assertRaisesRegex(DomainError, "MEAL_ALREADY_REMOVED"):
            _insert_void(tx, 5, 1, "Correction", datetime(2026, 10, 25))


class SettingsApiTests(ApiTestCase):
    def setUp(self):
        self.harness = ApiHarness()
        self.client = self.harness.client
        self.addCleanup(self.client.close)
        self.catalog = self.harness.services.catalog
        self.harness.staff.update = Mock(return_value={"staff_id": 7, "sessions_ended": 1})
        self.harness.staff.set_roles = Mock()
        self.catalog.update_department.return_value = {"id": 6, "name": "Auditorium"}
        self.catalog.department_usage.return_value = {"id": 6, "active_employees": 3}
        self.catalog.set_department_active.return_value = {"id": 6, "is_active": False, "active_employees": 3}
        for name in ("update_location", "update_scanner", "update_meal_type"):
            getattr(self.catalog, name).return_value = {"id": 8, "code": "C8", "name": "Counter"}
        for name in ("set_location_active", "set_scanner_active", "set_meal_type_active"):
            getattr(self.catalog, name).return_value = {"id": 8, "is_active": False}

    def patch(self, path, body):
        return self.client.patch(path, json=body, headers=self.harness.headers())

    def test_admin_routes_call_the_matching_services(self):
        self.harness.login()
        self.assertEqual(self.patch("/api/catalog/departments/6", {"name": "Auditorium"}).status_code, 200)
        self.assertEqual(self.client.get("/api/catalog/departments/6/usage").json()["active_employees"], 3)
        self.patch("/api/catalog/departments/6/active", {"is_active": False, "confirm": True})
        self.assertEqual(self.catalog.set_department_active.call_args.args[1:], (6, False, True))
        for kind, update, status in (("locations", "update_location", "set_location_active"),
                                     ("scanners", "update_scanner", "set_scanner_active"),
                                     ("meal-types", "update_meal_type", "set_meal_type_active")):
            with self.subTest(kind=kind):
                self.assertEqual(self.patch(f"/api/catalog/{kind}/8", {"code": "C8", "name": "Counter"}).status_code, 200)
                self.assertEqual(getattr(self.catalog, update).call_args.args[1:], (8, "C8", "Counter"))
                self.assertEqual(self.patch(f"/api/catalog/{kind}/8/active", {"is_active": False}).status_code, 200)
                self.assertEqual(getattr(self.catalog, status).call_args.args[1:], (8, False))
        self.assertEqual(self.patch("/api/staff/7", {"display_name": "Asha", "email": "asha@example.com"}).json()["sessions_ended"], 1)
        self.assertEqual(self.patch("/api/staff/7/roles", {"roles": ["WAITER", "ADMIN"]}).json(), {"staff_id": 7, "roles": ["ADMIN", "WAITER"]})
        self.assertEqual(self.harness.staff.set_roles.call_args.args[1:], (7, ["WAITER", "ADMIN"]))

    def test_invalid_kinds_bodies_and_confirm_outside_departments_are_rejected(self):
        self.harness.login()
        for path, body in (("/api/catalog/staff/8", {"code": "C", "name": "N"}),
                           ("/api/catalog/locations/8", {"name": "No code"}),
                           ("/api/catalog/locations/0", {"code": "C", "name": "N"}),
                           ("/api/catalog/locations/8/active", {"is_active": "false"}),
                           ("/api/catalog/departments/6", {"name": "A", "code": "extra"}),
                           ("/api/staff/7/roles", {"roles": ["OWNER"]}),
                           ("/api/staff/7", {"display_name": "Asha"})):
            with self.subTest(path=path, body=body):
                self.assertIn(self.patch(path, body).status_code, {404, 422})
        self.assert_error(self.patch("/api/catalog/scanners/8/active", {"is_active": False, "confirm": True}), 400, "INVALID_INPUT")
        self.catalog.set_scanner_active.assert_not_called()

    def test_conflicts_return_409_with_clear_messages(self):
        self.harness.login()
        cases = (
            ("set_department_active", "/api/catalog/departments/6/active", {"is_active": False}, "DEPARTMENT_HAS_ACTIVE_EMPLOYEES", "active employees"),
            ("update_scanner", "/api/catalog/scanners/3", {"code": "NEW", "name": "Meal Scanner"}, "SHARED_SCANNER_SETTING_PROTECTED", "Meal Scanner uses this item"),
            ("update_location", "/api/catalog/locations/8", {"code": "TAKEN", "name": "Hall"}, "LOCATION_CODE_EXISTS", "already uses that code"),
        )
        for method, path, body, code, text in cases:
            getattr(self.catalog, method).side_effect = DomainError(code)
            with self.subTest(code=code):
                response = self.patch(path, body)
                self.assert_error(response, 409, code)
                self.assertIn(text, response.json()["error"]["message"])

    def test_waiters_cannot_edit_or_deactivate_settings(self):
        self.harness.login("waiter")
        for path, body in (("/api/catalog/departments/6", {"name": "A"}), ("/api/catalog/scanners/8/active", {"is_active": False}),
                           ("/api/staff/7", {"display_name": "A", "email": "a@example.com"}), ("/api/staff/7/roles", {"roles": ["ADMIN"]})):
            with self.subTest(path=path):
                self.assert_error(self.patch(path, body), 403, "ROLE_REQUIRED")
        self.assert_error(self.client.get("/api/catalog/departments/6/usage"), 403, "ROLE_REQUIRED")
        self.catalog.update_department.assert_not_called()
        self.harness.staff.update.assert_not_called()
