import unittest
from contextlib import contextmanager
from datetime import datetime
from unittest.mock import Mock, patch

from meal_management.employees import EmployeeService
from meal_management.errors import DomainError
from meal_management.meals import MealService
from meal_management.models import Actor, ServerContext
from meal_management.security import generate_token


class TransactionDatabase:
    def __init__(self, tx):
        self.tx = tx
        self.committed = False
        self.rolled_back = False

    @contextmanager
    def transaction(self):
        try:
            yield self.tx
            self.committed = True
        except BaseException:
            self.rolled_back = True
            raise


class EmployeeRemovalTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 11, 9, 30)
        self.tx = Mock()
        self.tx.now.return_value = self.now
        self.db = TransactionDatabase(self.tx)
        self.qr = Mock()
        self.service = EmployeeService(self.db, self.qr)
        self.context = ServerContext(generate_token())
        self.actor = Actor(7, frozenset({"ADMIN"}))
        self.employee = {
            "id": 41,
            "employee_code": "EMP-41",
            "full_name": "Asha Rao",
            "email": "asha@example.test",
            "is_active": True,
        }

    def test_removal_archives_deactivates_and_revokes_in_one_transaction(self):
        credential = {"id": 72, "revoked_at": None}
        self.tx.one.side_effect = [self.employee, None, credential]
        with patch("meal_management.employees.require_actor", return_value=self.actor), patch("meal_management.employees.audit") as audit:
            result = self.service.archive(self.context, 41, "Duplicate record")
        self.assertEqual(result, self.now)
        self.assertTrue(self.db.committed)
        self.assertIn("INSERT INTO employee_archives", self.tx.insert.call_args.args[0])
        self.assertEqual(self.tx.insert.call_args.args[1], (41, 7, "Duplicate record", self.now))
        self.assertEqual(self.tx.execute.call_args.args[1], (False, self.now, 41))
        self.qr._revoke.assert_called_once_with(self.tx, 7, credential, "Duplicate record")
        self.assertEqual(audit.call_args.args[2], "EMPLOYEE_REMOVED")

    def test_already_removed_employee_cannot_be_removed_again(self):
        self.tx.one.side_effect = [self.employee, {"employee_id": 41}]
        with patch("meal_management.employees.require_actor", return_value=self.actor):
            with self.assertRaises(DomainError) as caught:
                self.service.archive(self.context, 41, "Second removal")
        self.assertEqual(caught.exception.code, "EMPLOYEE_ALREADY_REMOVED")
        self.assertTrue(self.db.rolled_back)
        self.tx.insert.assert_not_called()
        self.tx.execute.assert_not_called()
        self.qr._revoke.assert_not_called()

    def test_archived_employee_cannot_be_reactivated(self):
        self.qr._lock_employee.return_value = self.employee
        self.tx.one.return_value = {"employee_id": 41}
        with patch("meal_management.employees.require_actor", return_value=self.actor):
            with self.assertRaises(DomainError) as caught:
                self.service.set_active(self.context, 41, True)
        self.assertEqual(caught.exception.code, "EMPLOYEE_REMOVED")
        self.tx.execute.assert_not_called()


class MealRemovalTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 11, 10, 0)
        self.tx = Mock()
        self.tx.now.return_value = self.now
        self.db = TransactionDatabase(self.tx)
        self.service = MealService(self.db)
        self.context = ServerContext(generate_token())
        self.actor = Actor(7, frozenset({"ADMIN"}))
        self.meal = {
            "id": 91,
            "serving_id": 77,
            "unit_number": 1,
            "served_at": self.now,
            "kind": "EMPLOYEE",
        }

    def test_removal_creates_immutable_void_and_audit_event(self):
        self.tx.one.side_effect = [self.meal, None]
        with patch("meal_management.meals.require_actor", return_value=self.actor), patch("meal_management.meals.audit") as audit:
            result = self.service.void(self.context, 91, "Recorded by mistake")
        self.assertEqual(result, self.now)
        self.assertTrue(self.db.committed)
        self.assertIn("INSERT INTO meal_voids", self.tx.insert.call_args.args[0])
        self.assertEqual(self.tx.insert.call_args.args[1], (91, 7, "Recorded by mistake", self.now))
        self.assertEqual(audit.call_args.args[2], "MEAL_REMOVED")

    def test_already_removed_meal_cannot_be_removed_again(self):
        self.tx.one.side_effect = [self.meal, {"meal_id": 91}]
        with patch("meal_management.meals.require_actor", return_value=self.actor):
            with self.assertRaises(DomainError) as caught:
                self.service.void(self.context, 91, "Second removal")
        self.assertEqual(caught.exception.code, "MEAL_ALREADY_REMOVED")
        self.assertTrue(self.db.rolled_back)
        self.tx.insert.assert_not_called()

    def test_invalid_reason_is_rejected_before_database_access(self):
        with self.assertRaises(DomainError) as caught:
            self.service.void(self.context, 91, "   ")
        self.assertEqual(caught.exception.code, "INVALID_REMOVAL_REASON")
        self.assertFalse(self.db.committed)
        self.assertFalse(self.db.rolled_back)


if __name__ == "__main__":
    unittest.main()
