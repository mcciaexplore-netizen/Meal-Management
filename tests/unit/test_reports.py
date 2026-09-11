import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, Mock, patch

from meal_management.errors import DomainError
from meal_management.models import Actor, ServerContext
from meal_management.reports import ReportService
from meal_management.security import generate_token


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tx = Mock()
        self.db = MagicMock()
        self.db.transaction.return_value.__enter__.return_value = self.tx
        self.service = ReportService(self.db)
        self.context = ServerContext(generate_token())
        self.start = datetime(2026, 9, 9, tzinfo=timezone.utc)
        self.end = self.start + timedelta(days=1)
        self.auth_patch = patch(
            "meal_management.reports.require_actor",
            return_value=Actor(42, frozenset({"AUDITOR"})),
        )
        self.require_actor = self.auth_patch.start()
        self.addCleanup(self.auth_patch.stop)

    def test_history_normalizes_offset_and_uses_half_open_range(self):
        offset = timezone(timedelta(hours=5, minutes=30))
        start = datetime(2026, 9, 9, 5, 30, tzinfo=offset)
        end = start + timedelta(days=1)
        self.tx.all.return_value = []
        self.service.employee_history(self.context, 12, start, end)
        sql, params = self.tx.all.call_args.args
        self.assertEqual(
            params,
            (12, datetime(2026, 9, 9), datetime(2026, 9, 10), 0, 100),
        )
        self.assertIn("m.served_at >= %s AND m.served_at < %s", sql)
        self.assertIn("NOT EXISTS (SELECT 1 FROM meal_voids", sql)
        self.assertNotIn("2026-09-09", sql)
        self.require_actor.assert_called_once_with(
            self.tx, self.context, {"ADMIN", "AUDITOR"}
        )

    def test_history_returns_individual_meals_with_aware_utc_times(self):
        self.tx.all.return_value = [
            {"meal_id": 21, "served_at": datetime(2026, 9, 9, 9, 0)},
            {"meal_id": 22, "served_at": datetime(2026, 9, 9, 9, 1)},
        ]
        result = self.service.employee_history(
            self.context, 12, self.start, self.end, limit=2, after_id=20
        )
        self.assertEqual([row["meal_id"] for row in result], [21, 22])
        self.assertTrue(all(row["served_at"].tzinfo == timezone.utc for row in result))
        sql, params = self.tx.all.call_args.args
        self.assertEqual(params[-2:], (20, 2))
        self.assertIn("m.id > %s", sql)
        self.assertIn("ORDER BY m.id ASC LIMIT %s", sql)

    def test_naive_dates_are_rejected_before_database_access(self):
        with self.assertRaises(DomainError) as caught:
            self.service.employee_history(
                self.context, 12, self.start.replace(tzinfo=None), self.end
            )
        self.assertEqual(caught.exception.code, "UTC_OFFSET_REQUIRED")
        self.db.transaction.assert_not_called()

    def test_invalid_or_empty_intervals_are_rejected(self):
        for start, end in (
            (self.start, self.start),
            (self.end, self.start),
            ("2026-09-09", self.end),
            (self.start, None),
        ):
            with self.subTest(start=start, end=end):
                with self.assertRaises(DomainError) as caught:
                    self.service.meal_report(self.context, start, end)
                self.assertEqual(caught.exception.code, "INVALID_DATE_RANGE")
        self.db.transaction.assert_not_called()

    def test_invalid_employee_identifiers_are_rejected(self):
        for employee_id in (0, -1, True, "12", 1.5):
            with self.subTest(employee_id=employee_id):
                with self.assertRaises(DomainError) as caught:
                    self.service.employee_history(
                        self.context, employee_id, self.start, self.end
                    )
                self.assertEqual(caught.exception.code, "INVALID_EMPLOYEE_ID")
        self.db.transaction.assert_not_called()

    def test_invalid_pagination_is_rejected(self):
        for limit, after_id, error in (
            (0, 0, "INVALID_REPORT_LIMIT"),
            (1001, 0, "INVALID_REPORT_LIMIT"),
            (True, 0, "INVALID_REPORT_LIMIT"),
            (100, -1, "INVALID_REPORT_CURSOR"),
            (100, True, "INVALID_REPORT_CURSOR"),
            (100, "1", "INVALID_REPORT_CURSOR"),
        ):
            with self.subTest(limit=limit, after_id=after_id):
                with self.assertRaises(DomainError) as caught:
                    self.service.employee_history(
                        self.context, 12, self.start, self.end, limit, after_id
                    )
                self.assertEqual(caught.exception.code, error)
        self.db.transaction.assert_not_called()

    def test_denied_history_access_does_not_query_meals(self):
        self.require_actor.side_effect = DomainError("ROLE_REQUIRED")
        with self.assertRaises(DomainError) as caught:
            self.service.employee_history(self.context, 12, self.start, self.end)
        self.assertEqual(caught.exception.code, "ROLE_REQUIRED")
        self.tx.all.assert_not_called()

    def test_denied_aggregate_access_does_not_query_meals(self):
        self.require_actor.side_effect = DomainError("AUTHENTICATION_REQUIRED")
        with self.assertRaises(DomainError) as caught:
            self.service.meal_report(self.context, self.start, self.end)
        self.assertEqual(caught.exception.code, "AUTHENTICATION_REQUIRED")
        self.tx.all.assert_not_called()

    def test_report_separates_meal_counts_from_serving_counts(self):
        self.tx.all.return_value = [
            {
                "section": "TOTAL", "group_id": None, "code": None, "name": None,
                "meal_count": Decimal(5), "serving_count": Decimal(3),
            },
            {
                "section": "KIND", "group_id": None, "code": "EMPLOYEE", "name": "EMPLOYEE",
                "meal_count": Decimal(2), "serving_count": Decimal(2),
            },
            {
                "section": "KIND", "group_id": None, "code": "MASTER", "name": "MASTER",
                "meal_count": Decimal(3), "serving_count": Decimal(1),
            },
            {
                "section": "MEAL_TYPE", "group_id": 2, "code": "LUNCH", "name": "Lunch",
                "meal_count": Decimal(5), "serving_count": Decimal(3),
            },
            {
                "section": "AUTHORIZING_ADMIN", "group_id": 10, "code": None, "name": "Mira",
                "meal_count": Decimal(3), "serving_count": Decimal(1),
            },
        ]
        result = self.service.meal_report(self.context, self.start, self.end)
        self.assertEqual(
            result["totals"],
            {"meal_count": 5, "serving_count": 3, "employee_meals": 2, "visitor_meals": 3},
        )
        self.assertEqual(
            result["by_meal_type"],
            [{"meal_type_id": 2, "code": "LUNCH", "name": "Lunch", "meal_count": 5, "serving_count": 3}],
        )
        self.assertEqual(result["by_authorizing_admin"][0]["admin_id"], 10)
        self.assertEqual(result["by_authorizing_admin"][0]["meal_count"], 3)
        self.assertEqual(result["start"], self.start)
        self.assertEqual(result["end"], self.end)
        self.require_actor.assert_called_once_with(
            self.tx, self.context, {"ADMIN", "AUDITOR"}
        )

    def test_empty_report_has_zero_totals(self):
        self.tx.all.return_value = [
            {
                "section": "TOTAL", "group_id": None, "code": None, "name": None,
                "meal_count": 0, "serving_count": 0,
            }
        ]
        result = self.service.meal_report(self.context, self.start, self.end)
        self.assertEqual(
            result["totals"],
            {"meal_count": 0, "serving_count": 0, "employee_meals": 0, "visitor_meals": 0},
        )
        self.assertEqual(result["by_kind"], [])

    def test_direct_visitors_count_in_totals_without_an_authorizing_admin(self):
        self.tx.all.return_value = [
            {
                "section": "TOTAL", "group_id": None, "code": None, "name": None,
                "meal_count": 4, "serving_count": 3,
            },
            {
                "section": "KIND", "group_id": None, "code": "MASTER", "name": "MASTER",
                "meal_count": 4, "serving_count": 3,
            },
            {
                "section": "WAITER", "group_id": 22, "code": None, "name": "Casey Example",
                "meal_count": 4, "serving_count": 3,
            },
            {
                "section": "AUTHORIZING_ADMIN", "group_id": 7, "code": None, "name": "Admin Example",
                "meal_count": 2, "serving_count": 1,
            },
        ]
        result = self.service.meal_report(self.context, self.start, self.end)
        self.assertEqual(result["totals"]["visitor_meals"], 4)
        self.assertEqual(result["totals"]["meal_count"], 4)
        self.assertEqual(result["totals"]["serving_count"], 3)
        self.assertEqual(result["by_waiter"][0]["meal_count"], 4)
        self.assertEqual(result["by_authorizing_admin"][0]["meal_count"], 2)
        sql = self.tx.all.call_args.args[0]
        filtered, authorizing = sql.split("SELECT 'AUTHORIZING_ADMIN'", 1)
        self.assertIn("LEFT JOIN visitor_authorizations", filtered)
        self.assertNotIn("authorized_by IS NOT NULL", filtered)
        self.assertIn("WHERE kind = 'MASTER' AND authorized_by IS NOT NULL", authorizing)

    def test_report_uses_one_statement_for_all_aggregate_snapshots(self):
        self.tx.all.return_value = []
        self.service.meal_report(self.context, self.start, self.end)
        self.tx.all.assert_called_once()
        sql, params = self.tx.all.call_args.args
        self.assertIn("WITH filtered_meals AS", sql)
        self.assertIn("COUNT(DISTINCT serving_id)", sql)
        self.assertIn("NOT EXISTS (SELECT 1 FROM meal_voids", sql)
        self.assertEqual(params, (datetime(2026, 9, 9), datetime(2026, 9, 10)))


if __name__ == "__main__":
    unittest.main()
