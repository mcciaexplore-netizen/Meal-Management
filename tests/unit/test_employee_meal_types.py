import unittest
from unittest.mock import Mock, patch

from meal_management.employees import EmployeeService
from meal_management.errors import DomainError
from test_api import ApiTestCase


class BulkMealTypeInputTests(unittest.TestCase):
    def setUp(self):
        self.service = EmployeeService(Mock(), Mock())

    def test_bad_selections_are_refused_before_the_database(self):
        cases = (
            (lambda: self.service.preview_meal_types_bulk(Mock(), [], [2]), "INVALID_EMPLOYEE_IDS"),
            (lambda: self.service.preview_meal_types_bulk(Mock(), list(range(1, 502)), [2]), "INVALID_EMPLOYEE_IDS"),
            (lambda: self.service.preview_meal_types_bulk(Mock(), [1, 1], [2]), "INVALID_EMPLOYEE_IDS"),
            (lambda: self.service.preview_meal_types_bulk(Mock(), [1], None), "INVALID_MEAL_TYPE_IDS"),
            (lambda: self.service.preview_meal_types_bulk(Mock(), [1], []), "INVALID_MEAL_TYPE_IDS"),
            (lambda: self.service.set_meal_types_bulk(Mock(), [1, 2], [2], 3), "INVALID_EXPECTED_CHANGES"),
            (lambda: self.service.set_meal_types_bulk(Mock(), [1], [2], -1), "INVALID_EXPECTED_CHANGES"),
            (lambda: self.service.set_meal_types_bulk(Mock(), [1], [2], True), "INVALID_EXPECTED_CHANGES"),
        )
        for call, code in cases:
            with self.subTest(code=code), self.assertRaisesRegex(DomainError, code):
                call()
        self.service.db.transaction.assert_not_called()

    def test_five_hundred_employees_are_accepted(self):
        transaction = Mock()
        transaction.all.return_value = []
        self.service.db.transaction.return_value.__enter__ = Mock(return_value=transaction)
        self.service.db.transaction.return_value.__exit__ = Mock(return_value=False)
        self.service._meal_type_plan = Mock(return_value=([{"id": n} for n in range(1, 501)], {}, []))
        with patch("meal_management.employees.require_actor"):
            result = self.service.preview_meal_types_bulk(Mock(), list(range(500, 0, -1)), [4, 2])
        self.assertEqual((result["selected"], result["changing"]), (500, 0))
        self.assertEqual(self.service._meal_type_plan.call_args.args[1], tuple(range(1, 501)))
        self.assertEqual(self.service._meal_type_plan.call_args.args[2], (2, 4))


class BulkMealTypeApiTests(ApiTestCase):
    def test_routes_pass_selection_and_previewed_count(self):
        self.harness.login()
        employees = self.harness.services.employees
        employees.preview_meal_types_bulk.return_value = {"selected": 2, "changing": 1, "unchanged": 1, "examples": []}
        employees.set_meal_types_bulk.return_value = {"selected": 2, "changed": 1, "meal_type_ids": [2, 4]}
        response = self.harness.post("/api/employees/meal-types/preview", json={"employee_ids": [7, 8], "meal_type_ids": [2, 4]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(employees.preview_meal_types_bulk.call_args.args[1:], ([7, 8], [2, 4]))
        response = self.harness.post("/api/employees/meal-types", json={"employee_ids": [7, 8], "meal_type_ids": [2, 4], "expected_changes": 1})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(employees.set_meal_types_bulk.call_args.args[1:], ([7, 8], [2, 4], 1))

    def test_inputs_are_bounded_and_the_count_must_be_sent(self):
        self.harness.login()
        for path, body in (
            ("/api/employees/meal-types/preview", {"employee_ids": list(range(1, 502)), "meal_type_ids": [2]}),
            ("/api/employees/meal-types/preview", {"employee_ids": [1], "meal_type_ids": []}),
            ("/api/employees/meal-types", {"employee_ids": [1], "meal_type_ids": [2]}),
            ("/api/employees/meal-types", {"employee_ids": [1], "meal_type_ids": [2], "expected_changes": 501}),
        ):
            with self.subTest(path=path, keys=sorted(body)):
                self.assert_error(self.harness.post(path, json=body), 422, "INVALID_INPUT")
        self.harness.services.employees.set_meal_types_bulk.assert_not_called()

    def test_stale_preview_is_a_conflict_and_waiters_are_refused(self):
        self.harness.login()
        self.harness.services.employees.set_meal_types_bulk.side_effect = DomainError("EMPLOYEE_MEAL_TYPES_COUNT_CHANGED")
        response = self.harness.post("/api/employees/meal-types", json={"employee_ids": [1], "meal_type_ids": [2], "expected_changes": 1})
        self.assert_error(response, 409, "EMPLOYEE_MEAL_TYPES_COUNT_CHANGED")
        self.assertIn("Preview again", response.text)
        self.harness.login("waiter")
        for path, body in (("/api/employees/meal-types/preview", {"employee_ids": [1], "meal_type_ids": [2]}),
                           ("/api/employees/meal-types", {"employee_ids": [1], "meal_type_ids": [2], "expected_changes": 0})):
            self.assert_error(self.harness.post(path, json=body), 403, "ROLE_REQUIRED")


if __name__ == "__main__":
    unittest.main()
