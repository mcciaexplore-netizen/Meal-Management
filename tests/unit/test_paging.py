import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from meal_management.errors import DomainError
from meal_management.models import Actor, ServerContext
from meal_management.paging import format_cursor, newest_first, next_cursor, parse_cursor
from meal_management.queries import QueryService
from meal_management.security import generate_token
from meal_management.waiter_shifts import WaiterShiftService


class CursorTests(unittest.TestCase):
    def test_cursor_round_trips_microseconds_and_id(self):
        moment = datetime(2026, 10, 26, 6, 15, 12, 123456)
        text = format_cursor(moment, 4812)
        self.assertEqual(text, "1792995312123456.4812")
        self.assertEqual(parse_cursor(text), (moment, 4812))
        self.assertEqual(format_cursor(moment.replace(tzinfo=timezone.utc), 4812), text)
        self.assertEqual(format_cursor(moment.replace(tzinfo=timezone(timedelta(hours=5, minutes=30))) + timedelta(hours=5, minutes=30), 4812), text)
        self.assertIsNone(parse_cursor(None))

    def test_malformed_cursors_are_refused(self):
        for value in ("", "12", "1.0", "-1.2", "1.2.3", "a.1", "1." + str(2**64), "1." + "1" * 21, " 1.2", 12, b"1.2"):
            with self.subTest(value=value), self.assertRaisesRegex(DomainError, "INVALID_REPORT_CURSOR"):
                parse_cursor(value)

    def test_keyset_condition_and_order(self):
        moment = datetime(2026, 1, 1)
        self.assertEqual(newest_first("m.served_at", "m.id", None), (None, (), " ORDER BY m.served_at DESC, m.id DESC LIMIT %s"))
        condition, values, _ = newest_first("m.served_at", "m.id", (moment, 9))
        self.assertEqual(condition, "(m.served_at < %s OR (m.served_at = %s AND m.id < %s))")
        self.assertEqual(values, (moment, moment, 9))

    def test_next_cursor_comes_from_the_last_returned_row(self):
        moment = datetime(2026, 1, 1)
        rows = [{"id": 5, "t": moment}, {"id": 4, "t": moment}, {"id": 3, "t": moment}]
        self.assertEqual(next_cursor(rows, 2, "t"), format_cursor(moment, 4))
        self.assertIsNone(next_cursor(rows, 3, "t"))


class ListOrderTests(unittest.TestCase):
    def setUp(self):
        self.db = MagicMock()
        self.tx = self.db.transaction.return_value.__enter__.return_value
        self.tx.all.return_value = []
        self.tx.now.return_value = datetime(2026, 9, 9, 12)
        self.context = ServerContext(generate_token())
        patcher = patch("meal_management.queries.require_actor", return_value=Actor(7, frozenset({"ADMIN"})))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.start = datetime(2026, 9, 1, tzinfo=timezone.utc)
        self.end = self.start + timedelta(days=2)

    def sql(self):
        return self.tx.all.call_args_list[0].args[0]

    def test_every_admin_list_is_newest_first_with_id_tie_breaker(self):
        service = QueryService(self.db)
        cursor = "1788948000250000.12"
        for call, order in (
            (lambda: service.staff(self.context, before=cursor), "s.created_at DESC, s.id DESC"),
            (lambda: service.list_employees(self.context, before=cursor), "e.created_at DESC, e.id DESC"),
            (lambda: service.master_qrs(self.context, before=cursor), "q.issued_at DESC, q.id DESC"),
            (lambda: service.email_status(self.context, before=cursor), "q.created_at DESC, q.id DESC"),
            (lambda: service.meal_history(self.context, self.start, self.end, before=cursor), "m.served_at DESC, m.id DESC"),
            (lambda: service.scans(self.context, self.start, self.end, before=cursor), "a.received_at DESC, a.id DESC"),
            (lambda: service.visitor_authorizations(self.context, status="all", before=cursor), "a.authorized_at DESC, a.id DESC"),
        ):
            with self.subTest(order=order):
                self.tx.all.reset_mock()
                call()
                sql = self.sql()
                self.assertTrue(sql.endswith(" ORDER BY " + order + " LIMIT %s"), sql[-120:])
                column, identifier = order.replace(" DESC", "").split(", ")
                self.assertIn(f"({column} < %s OR ({column} = %s AND {identifier} < %s))", sql)
                self.assertNotIn(identifier + " > %s", sql)

    def test_waiter_shifts_are_newest_started_first(self):
        with patch("meal_management.waiter_shifts.require_actor"):
            WaiterShiftService(self.db, MagicMock()).list(self.context, before="1788948000250000.12")
        sql, params = self.tx.all.call_args.args
        self.assertIn("(w.started_at < %s OR (w.started_at = %s AND w.id < %s))", sql)
        self.assertTrue(sql.endswith(" ORDER BY w.started_at DESC, w.id DESC LIMIT %s"))
        self.assertEqual(params[-2:], (12, 51))


if __name__ == "__main__":
    unittest.main()
