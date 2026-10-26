import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock
from uuid import uuid4

from meal_management.config import Settings
from meal_management.errors import ConfigurationError, DomainError
from meal_management.meals import MealService, MealTypeLocked
from test_api import ApiHarness, ApiTestCase


def database_environment(**overrides):
    from cryptography.fernet import Fernet

    values = {"DB_HOST": "127.0.0.1", "DB_NAME": "meal_management", "DB_USER": "meal_app",
              "DB_PASSWORD": "fictional-password", "QR_ENCRYPTION_KEYS": Fernet.generate_key().decode()}
    values.update(overrides)
    return values


class MealTypeRuleTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 26, 6, 0)
        self.tx = Mock()

    def test_ineligible_type_is_refused_before_the_lock_check(self):
        self.tx.one.return_value = None
        with self.assertRaisesRegex(DomainError, "MEAL_TYPE_NOT_ELIGIBLE"):
            MealService(Mock())._check_employee_meal_type(self.tx, 21, 3, self.now)
        self.assertEqual(self.tx.one.call_count, 1)

    def test_same_type_within_the_window_is_locked_with_the_earlier_time(self):
        earlier = datetime(2026, 10, 26, 2, 45, 30, 123456)
        self.tx.one.side_effect = [{"employee_id": 21}, {"served_at": earlier}]
        with self.assertRaises(MealTypeLocked) as caught:
            MealService(Mock(), lock_hours=12)._check_employee_meal_type(self.tx, 21, 3, self.now)
        self.assertEqual(caught.exception.code, "MEAL_TYPE_ALREADY_RECORDED")
        self.assertEqual(caught.exception.served_at, "2026-10-26T02:45:30Z")
        sql, params = self.tx.one.call_args.args
        self.assertEqual(params, (21, self.now - timedelta(hours=12), 3))
        self.assertIn("meal_type_corrections", sql)
        self.assertIn("NOT EXISTS (SELECT 1 FROM meal_voids", sql)

    def test_lock_can_be_turned_off_and_is_bounded(self):
        self.tx.one.return_value = {"employee_id": 21}
        MealService(Mock(), lock_hours=0)._check_employee_meal_type(self.tx, 21, 3, self.now)
        self.assertEqual(self.tx.one.call_count, 1)
        for hours in (-1, 49, "12", None):
            with self.subTest(hours=hours), self.assertRaises(DomainError):
                MealService(Mock(), lock_hours=hours)

    def test_lock_hours_setting(self):
        self.assertEqual(Settings.from_env(database_environment()).meal_type_lock_hours, 12)
        self.assertEqual(Settings.from_env(database_environment(MEAL_TYPE_LOCK_HOURS="0")).meal_type_lock_hours, 0)
        for value in ("-1", "49", "twelve"):
            with self.subTest(value=value), self.assertRaisesRegex(ConfigurationError, "MEAL_TYPE_LOCK_HOURS"):
                Settings.from_env(database_environment(MEAL_TYPE_LOCK_HOURS=value))

    def test_corrections_validate_input_before_database_access(self):
        database = Mock()
        service = MealService(database)
        for meal_id, meal_type_id, reason, code in ((0, 3, "Reason", "INVALID_MEAL_ID"), (5, 0, "Reason", "INVALID_MEAL_TYPE_ID"),
                                                    (5, 3, " ", "INVALID_CORRECTION_REASON"), (5, 3, "x" * 256, "INVALID_CORRECTION_REASON")):
            with self.subTest(code=code), self.assertRaisesRegex(DomainError, code):
                service.correct_meal_type(object(), meal_id, meal_type_id, reason)
        database.transaction.assert_not_called()


class MealTypeApiTests(ApiTestCase):
    def setUp(self):
        self.harness = ApiHarness()
        self.client = self.harness.client
        self.addCleanup(self.client.close)
        self.harness.services.meals.correct_meal_type = Mock(return_value={"meal_id": 9, "meal_type_id": 3, "corrected_at": "2026-10-26T06:00:00"})
        self.harness.services.employees.set_meal_types.return_value = {"employee_id": 7, "meal_type_ids": [2, 3]}

    def patch(self, path, body):
        return self.client.patch(path, json=body, headers=self.harness.headers())

    def test_admin_corrects_meal_types_and_sets_eligibility(self):
        self.harness.login()
        response = self.harness.post("/api/meals/9/meal-type", json={"meal_type_id": 3, "reason": "Wrong service chosen"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.harness.services.meals.correct_meal_type.call_args.args[1:], (9, 3, "Wrong service chosen"))
        response = self.patch("/api/employees/7/meal-types", {"meal_type_ids": [3, 2]})
        self.assertEqual(response.json()["meal_type_ids"], [2, 3])
        self.assertEqual(self.harness.services.employees.set_meal_types.call_args.args[1:], (7, [3, 2]))
        for path, body in (("/api/meals/9/meal-type", {"meal_type_id": 3}), ("/api/meals/9/meal-type", {"meal_type_id": 3, "reason": ""}),
                           ("/api/meals/0/meal-type", {"meal_type_id": 3, "reason": "x"})):
            with self.subTest(body=body):
                self.assertEqual(self.harness.post(path, json=body).status_code, 422)
        self.assertEqual(self.patch("/api/employees/7/meal-types", {"meal_type_ids": []}).status_code, 422)
        self.harness.services.meals.correct_meal_type.side_effect = DomainError("MEAL_TYPE_UNCHANGED")
        self.assert_error(self.harness.post("/api/meals/9/meal-type", json={"meal_type_id": 3, "reason": "Same"}), 400, "MEAL_TYPE_UNCHANGED")

    def test_waiters_cannot_correct_meals_or_change_eligibility(self):
        self.harness.login("waiter")
        self.assert_error(self.harness.post("/api/meals/9/meal-type", json={"meal_type_id": 3, "reason": "x"}), 403, "ROLE_REQUIRED")
        self.assert_error(self.patch("/api/employees/7/meal-types", {"meal_type_ids": [3]}), 403, "ROLE_REQUIRED")
        self.harness.services.meals.correct_meal_type.assert_not_called()

    def test_meal_history_filters_by_meal_type(self):
        self.harness.login()
        response = self.client.get("/api/reports/meals", params={"start": "2026-10-01T00:00:00Z", "end": "2026-10-02T00:00:00Z", "meal_type_id": 3})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.harness.queries.meal_history.call_args.kwargs["meal_type_id"], 3)

    def test_scanner_lists_meal_types_and_requires_one_per_scan(self):
        harness = ApiHarness(application="scanner")
        self.addCleanup(harness.client.close)
        harness.app.state.scanner = Mock()
        harness.app.state.scanner.meal_types.return_value = {"meal_types": [{"id": 3, "code": "LUNCH", "name": "Lunch"}]}
        session = harness.client.get("/api/scanner/session").json()
        self.assertEqual(harness.client.get("/api/scanner/meal-types").json()["meal_types"][0]["name"], "Lunch")
        headers = {"Origin": harness.runtime.app_origin, "X-CSRF-Token": session["csrf_token"]}
        response = harness.client.post("/api/scanner/read", json={"request_id": str(uuid4()), "token": "t" * 43}, headers=headers)
        self.assertEqual(response.status_code, 422)
        harness.app.state.scanner.read.assert_not_called()
