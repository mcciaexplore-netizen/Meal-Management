import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

from meal_management.errors import DomainError
from meal_management.models import Actor, ServerContext
from meal_management.queries import QueryService
from meal_management.security import generate_token


class QueryServiceTests(unittest.TestCase):
    def setUp(self):
        self.db = MagicMock()
        self.tx = self.db.transaction.return_value.__enter__.return_value
        self.service = QueryService(self.db)
        self.context = ServerContext(generate_token())
        self.now = datetime(2026, 9, 9, 12)
        self.tx.now.return_value = self.now
        self.start = self.now.replace(tzinfo=timezone.utc)
        self.end = self.start + timedelta(days=1)
        self.tx.all.return_value = []
        self.actor = Actor(7, frozenset({"ADMIN"}))
        self.auth = patch("meal_management.queries.require_actor", return_value=self.actor)
        self.require_actor = self.auth.start()
        self.addCleanup(self.auth.stop)

    def test_employee_list_uses_keyset_pagination_and_extra_row(self):
        self.tx.all.return_value = [{"id": 11, "is_active": 1}, {"id": 12}, {"id": 13}]
        result = self.service.list_employees(self.context, limit=2, after_id=10)
        self.assertEqual([item["id"] for item in result["items"]], [11, 12])
        self.assertEqual(result["next_cursor"], 12)
        self.assertIs(result["items"][0]["is_active"], True)
        sql, params = self.tx.all.call_args.args
        self.assertIn("e.id > %s", sql)
        self.assertIn("NOT EXISTS (SELECT 1 FROM employee_archives", sql)
        self.assertIn("ORDER BY e.id LIMIT %s", sql)
        self.assertEqual(params, [10, 3])

    def test_staff_list_excludes_password_hash_and_parses_roles(self):
        self.tx.all.return_value = [{"id": 7, "role_codes": "ADMIN,WAITER", "is_active": 1}]
        result = self.service.staff(self.context)
        self.assertEqual(result["items"][0]["roles"], ["ADMIN", "WAITER"])
        self.assertNotIn("role_codes", result["items"][0])
        self.assertNotIn("password", self.tx.all.call_args.args[0])

    def test_search_text_is_parameterized_and_wildcards_are_escaped(self):
        search = "a%' OR 1=1 _="
        self.service.list_employees(self.context, q=search, active=False)
        sql, params = self.tx.all.call_args.args
        self.assertNotIn(search, sql)
        self.assertEqual(params[1], "%a=%' OR 1==1 =_==%")
        self.assertEqual(params[4], False)
        self.assertEqual(sql.count("%s"), len(params))

    def test_short_page_has_no_next_cursor(self):
        self.tx.all.return_value = [{"id": 3}]
        result = self.service.master_qrs(self.context, limit=2)
        self.assertIsNone(result["next_cursor"])

    def test_empty_page_has_no_next_cursor(self):
        self.assertEqual(self.service.list_employees(self.context), {"items": [], "next_cursor": None})

    def test_employee_not_found_is_explicit(self):
        self.tx.one.return_value = None
        with self.assertRaises(DomainError) as caught:
            self.service.employee(self.context, 7)
        self.assertEqual(caught.exception.code, "EMPLOYEE_NOT_FOUND")

    def test_employee_metadata_converts_datetimes_to_utc(self):
        self.tx.one.return_value = {"id": 7, "created_at": self.now, "is_active": 1}
        result = self.service.employee(self.context, 7)
        self.assertEqual(result["created_at"], self.start)
        self.assertIs(result["created_at"].tzinfo, timezone.utc)

    def test_current_qr_returns_none_when_credential_missing(self):
        self.tx.one.side_effect = [{"id": 7}, None]
        self.assertIsNone(self.service.current_employee_qr(self.context, 7))

    def test_current_qr_exposes_expired_metadata_without_ciphertext(self):
        self.tx.one.side_effect = [{"id": 7}, {"id": 9, "kind": "EMPLOYEE", "expires_at": self.now}]
        result = self.service.current_employee_qr(self.context, 7)
        sql = self.tx.one.call_args.args[0]
        self.assertEqual(result["expires_at"], self.start)
        self.assertNotIn("token_ciphertext", sql)
        self.assertNotIn("token_hash", sql)

    def test_qr_metadata_never_selects_credential_material(self):
        self.tx.one.return_value = {"id": 9, "kind": "MASTER"}
        result = self.service.qr_metadata(self.context, 9)
        self.assertEqual(result["kind"], "MASTER")
        sql = self.tx.one.call_args.args[0]
        self.assertNotIn("token", sql)
        self.assertNotIn("SELECT *", sql)

    def test_missing_qr_metadata_is_explicit(self):
        self.tx.one.return_value = None
        with self.assertRaises(DomainError) as caught:
            self.service.qr_metadata(self.context, 9)
        self.assertEqual(caught.exception.code, "QR_NOT_FOUND")

    def test_email_status_sanitizes_errors_and_omits_payload(self):
        self.tx.all.return_value = [{"id": 9, "status": "FAILED", "last_error": "EMAIL_DELIVERY_CLAIMED_private-marker"}]
        result = self.service.email_status(self.context, employee_id=17)
        sql, params = self.tx.all.call_args.args
        self.assertEqual(result["items"][0]["status"], "NEEDS_REVIEW")
        self.assertEqual(result["items"][0]["code"], "EMAIL_DELIVERY_NEEDS_REVIEW")
        self.assertNotIn("private-marker", repr(result))
        self.assertNotIn("payload_ciphertext", sql)
        self.assertIn("c.employee_id = %s", sql)
        self.assertEqual(params, [0, 17, 51])

    def test_meal_history_returns_individual_meals_and_utc_uuids(self):
        identifier = uuid4()
        self.tx.all.return_value = [{"id": 8, "request_id": identifier.bytes, "served_at": self.now}]
        result = self.service.meal_history(self.context, self.start, self.end, employee_id=17)
        self.assertEqual(result["items"][0]["request_id"], str(identifier))
        self.assertEqual(result["items"][0]["served_at"], self.start)
        sql, params = self.tx.all.call_args.args
        self.assertIn("FROM meals m", sql)
        self.assertIn("m.served_at >= %s", sql)
        self.assertIn("m.served_at < %s", sql)
        self.assertIn("NOT EXISTS (SELECT 1 FROM meal_voids", sql)
        self.assertEqual(params, [self.now, self.now + timedelta(days=1), 0, 17, 51])

    def test_reports_normalize_non_utc_offsets(self):
        offset = timezone(timedelta(hours=5, minutes=30))
        self.service.scans(self.context, self.start.astimezone(offset), self.end.astimezone(offset))
        params = self.tx.all.call_args.args[1]
        self.assertEqual(params[:2], [self.now, self.now + timedelta(days=1)])

    def test_history_includes_direct_and_legacy_visitor_details(self):
        direct = {
            "id": 11, "kind": "MASTER", "visitor_company_name": "Example Workshop",
            "visitor_name": "Alex Example", "visitor_email": "alex@example.test",
            "visitor_phone": "+1 202 555 0123", "visitor_organization": None,
            "authorized_by": None,
        }
        legacy = {
            "id": 12, "kind": "MASTER", "visitor_company_name": "Example Partners",
            "visitor_name": "Morgan Example", "visitor_email": None,
            "visitor_phone": None, "visitor_organization": "Example Partners",
            "authorized_by": 7,
        }
        self.tx.all.return_value = [direct, legacy]
        result = self.service.meal_history(self.context, self.start, self.end)
        self.assertEqual(result["items"], [direct, legacy])
        sql, params = self.tx.all.call_args.args
        self.assertIn("COALESCE(s.visitor_company_name, a.visitor_organization) AS visitor_company_name", sql)
        self.assertIn("COALESCE(s.visitor_name, a.visitor_name) AS visitor_name", sql)
        self.assertIn("s.visitor_email, s.visitor_phone, a.visitor_organization", sql)
        self.assertIn("LEFT JOIN visitor_authorizations a ON a.id = s.authorization_id", sql)
        self.assertNotIn("a.authorized_by IS NOT NULL", sql)
        self.assertEqual(sql.count("%s"), len(params))

    def test_scan_report_omits_hashes_and_supports_outcome(self):
        self.service.scans(self.context, self.start, self.end, outcome="REJECTED")
        sql, params = self.tx.all.call_args.args
        self.assertNotIn("token_hash", sql)
        self.assertNotIn("payload_hash", sql)
        self.assertIn("a.outcome = %s", sql)
        self.assertEqual(params[-2], "REJECTED")

    def test_pending_visitor_details_are_reported_without_approval(self):
        identifier = uuid4()
        self.tx.all.return_value = [{
            "id": 13, "request_id": identifier.bytes, "outcome": "AWAITING_DETAILS",
            "serving_id": None, "rejection_code": None, "completed_at": self.now,
        }]
        result = self.service.scans(self.context, self.start, self.end, outcome="AWAITING_DETAILS")
        row = result["items"][0]
        self.assertEqual(row["outcome"], "AWAITING_DETAILS")
        self.assertIsNone(row["serving_id"])
        self.assertIsNone(row["rejection_code"])
        self.assertEqual(row["request_id"], str(identifier))
        sql, params = self.tx.all.call_args.args
        self.assertNotIn("AWAITING_DETAILS", sql)
        self.assertEqual(params[-2], "AWAITING_DETAILS")
        self.assertNotIn("scan_visitor_hash", sql)
        self.require_actor.assert_called_once_with(self.tx, self.context, {"ADMIN"})

    def test_waiter_catalog_exposes_no_other_staff_or_departments(self):
        self.require_actor.return_value = Actor(8, frozenset({"WAITER"}))
        result = self.service.catalog(self.context)
        self.assertEqual(result["waiters"], [])
        self.assertEqual(result["departments"], [])
        self.assertEqual(self.tx.all.call_count, 3)
        for call in self.tx.all.call_args_list:
            self.assertIn("is_active = 1", call.args[0])
            self.assertNotIn("staff_accounts", call.args[0])

    def test_admin_catalog_contains_waiters_and_inactive_configuration(self):
        result = self.service.catalog(self.context)
        self.assertEqual(set(result), {"departments", "meal_types", "locations", "scanners", "waiters"})
        self.assertEqual(self.tx.all.call_count, 5)
        self.assertIn("r.role_code IN ('ADMIN', 'WAITER')", self.tx.all.call_args.args[0])
        self.assertIn("SELECT DISTINCT", self.tx.all.call_args.args[0])

    def test_waiter_authorizations_are_scoped_to_authenticated_actor(self):
        self.require_actor.return_value = Actor(8, frozenset({"WAITER"}))
        self.service.visitor_authorizations(self.context)
        sql, params = self.tx.all.call_args.args
        self.assertIn("a.waiter_id = %s", sql)
        self.assertEqual(params, [0, 8, self.now, 51])
        self.assertNotIn("token", sql)

    def test_admin_can_list_all_authorization_statuses(self):
        self.service.visitor_authorizations(self.context, status="all")
        sql, params = self.tx.all.call_args.args
        self.assertNotIn("a.waiter_id = %s", sql)
        self.assertNotIn("a.expires_at > %s", sql)
        self.assertEqual(params, [0, 51])

    def test_unrestricted_queries_require_admin_role(self):
        operations = [
            lambda: self.service.staff(self.context),
            lambda: self.service.list_employees(self.context),
            lambda: self.service.employee(self.context, 7),
            lambda: self.service.current_employee_qr(self.context, 7),
            lambda: self.service.master_qrs(self.context),
            lambda: self.service.qr_metadata(self.context, 9),
            lambda: self.service.email_status(self.context),
            lambda: self.service.meal_history(self.context, self.start, self.end),
            lambda: self.service.scans(self.context, self.start, self.end),
        ]
        self.require_actor.side_effect = DomainError("ROLE_REQUIRED")
        for operation in operations:
            with self.subTest(operation=operation):
                with self.assertRaises(DomainError):
                    operation()
                self.require_actor.assert_called_with(self.tx, self.context, {"ADMIN"})
        self.tx.one.assert_not_called()
        self.tx.all.assert_not_called()

    def test_invalid_pagination_and_filters_do_not_access_database(self):
        operations = [
            lambda: self.service.list_employees(self.context, limit=True),
            lambda: self.service.list_employees(self.context, limit=1001),
            lambda: self.service.list_employees(self.context, after_id=-1),
            lambda: self.service.list_employees(self.context, after_id=2**64),
            lambda: self.service.list_employees(self.context, active="true"),
            lambda: self.service.employee(self.context, False),
            lambda: self.service.email_status(self.context, employee_id=0),
            lambda: self.service.scans(self.context, self.now, self.end),
            lambda: self.service.scans(self.context, self.end, self.start),
            lambda: self.service.scans(self.context, self.start, self.end, outcome="invalid"),
            lambda: self.service.visitor_authorizations(self.context, status="invalid"),
        ]
        for operation in operations:
            with self.subTest(operation=operation):
                with self.assertRaises(DomainError):
                    operation()
        self.db.transaction.assert_not_called()


if __name__ == "__main__":
    unittest.main()
