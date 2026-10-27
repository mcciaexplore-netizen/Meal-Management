import secrets
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

from test_mysql_services import MealServicesFixture

OFFSETS = (30, 10, 20, 20, 20)
EXPECTED = (0, 4, 3, 2, 1)


class NewestFirstMySQLTests(MealServicesFixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from meal_management.queries import QueryService

        cls.queries = QueryService(cls.db)

    def setUp(self):
        super().setUp()
        now = self.rows("SELECT UTC_TIMESTAMP(6) AS now")[0]["now"].replace(microsecond=0)
        self.base = now + timedelta(minutes=5)

    @contextmanager
    def at(self, seconds):
        with patch("meal_management.database.Session.now", return_value=self.base + timedelta(seconds=seconds)):
            yield

    def created(self, make):
        identifiers = []
        for offset in OFFSETS:
            with self.at(offset):
                identifiers.append(make(len(identifiers)))
        return identifiers

    def walk(self, fetch, limit=2):
        identifiers, cursor = [], None
        for _ in range(1000):
            page = fetch(limit=limit, before=cursor)
            identifiers.extend(item["id"] for item in page["items"])
            cursor = page["next_cursor"]
            if cursor is None:
                return identifiers
        self.fail("paging did not finish")

    def assert_newest_first(self, fetch, identifiers, expected_positions=EXPECTED):
        expected = [identifiers[index] for index in expected_positions]
        mine = set(identifiers)
        for limit in (1, 2, 3, 200):
            with self.subTest(limit=limit):
                listed = self.walk(fetch, limit)
                self.assertEqual(len(listed), len(set(listed)), "a row was repeated across pages")
                self.assertEqual([value for value in listed if value in mine], expected)

    def range(self):
        start = (self.base - timedelta(days=1)).replace(tzinfo=timezone.utc)
        return start, start + timedelta(days=3)

    def record_meal(self, token, meal_type_id=None):
        scan = self.scan(token)
        if meal_type_id is not None:
            scan = replace(scan, meal_type_id=meal_type_id)
        return self.meals.record(self.waiter_context, scan).meal_ids[0]

    def lunch(self):
        return self.scalar("SELECT id FROM meal_types WHERE code = 'LUNCH'")

    def test_meal_records_are_newest_first_with_ties_and_keep_the_meal_number(self):
        _, token = self.employee()
        meals = self.created(lambda _: self.record_meal(token))
        start, end = self.range()
        self.assert_newest_first(lambda **page: self.queries.meal_history(self.admin_context, start, end, **page), meals)
        first = self.queries.meal_history(self.admin_context, start, end, limit=1)["items"][0]
        self.assertEqual(first["meal_id"], first["id"])

    def test_meal_filters_continue_correctly_on_page_two(self):
        first, first_token = self.employee()
        second, second_token = self.employee()
        lunch = self.lunch()
        plan = ((first_token, None), (second_token, lunch), (first_token, lunch), (second_token, None), (first_token, None))
        meals = self.created(lambda index: self.record_meal(plan[index][0], plan[index][1]))
        start, end = self.range()
        order = [meals[index] for index in EXPECTED]
        for label, filters, members in (
            ("employee", {"employee_id": first.employee_id}, {meals[0], meals[2], meals[4]}),
            ("meal type", {"meal_type_id": lunch}, {meals[1], meals[2]}),
        ):
            with self.subTest(filter=label):
                listed = self.walk(lambda **page: self.queries.meal_history(self.admin_context, start, end, **filters, **page), 1)
                self.assertEqual([value for value in listed if value in set(meals)], [value for value in order if value in members])
        narrow_end = (self.base + timedelta(seconds=25)).replace(tzinfo=timezone.utc)
        listed = self.walk(lambda **page: self.queries.meal_history(self.admin_context, start, narrow_end, **page), 1)
        self.assertEqual([value for value in listed if value in set(meals)], [meals[index] for index in (4, 3, 2, 1)])

    def test_cursor_row_removed_or_filtered_out_still_continues_without_gaps(self):
        first, first_token = self.employee()
        _, second_token = self.employee()
        tokens = (first_token, second_token, first_token, first_token, second_token)
        meals = self.created(lambda index: self.record_meal(tokens[index]))
        start, end = self.range()
        cursor = None
        seen = []
        while meals[4] not in seen:
            page = self.queries.meal_history(self.admin_context, start, end, limit=1, before=cursor)
            seen.extend(item["id"] for item in page["items"])
            cursor = page["next_cursor"]
        cursor_after_removed_row = cursor
        cursor_row = meals[4]
        cursor_handle = self.admin_connection.cursor()
        try:
            cursor_handle.execute("INSERT INTO meal_voids (meal_id, voided_by_staff_id, reason) VALUES (%s, %s, 'Removed in test')",
                                  (cursor_row, self.admin_id))
        finally:
            cursor_handle.close()
        remaining = self.walk(lambda **page: self.queries.meal_history(
            self.admin_context, start, end, **{**page, "before": page["before"] or cursor_after_removed_row}))
        self.assertEqual([value for value in remaining if value in set(meals)], [meals[3], meals[2], meals[1]])
        filtered = self.walk(lambda **page: self.queries.meal_history(
            self.admin_context, start, end, employee_id=first.employee_id,
            **{**page, "before": page["before"] or cursor_after_removed_row}))
        self.assertEqual([value for value in filtered if value in set(meals)], [meals[3], meals[2]])

    def test_rejected_scans_are_newest_first_and_the_dashboard_gets_the_newest_five(self):
        def refused(_):
            self.meals.record(self.waiter_context, self.scan("unknown-" + uuid4().hex))
            return self.scalar("SELECT MAX(id) FROM scan_attempts")

        attempts = self.created(refused)
        start, end = self.range()
        self.assert_newest_first(lambda **page: self.queries.scans(self.admin_context, start, end, outcome="REJECTED", **page), attempts)
        newest = self.queries.scans(self.admin_context, start, end, outcome="REJECTED", limit=5)["items"]
        self.assertEqual([item["id"] for item in newest if item["id"] in set(attempts)], [attempts[index] for index in EXPECTED])
        narrow_end = (self.base + timedelta(seconds=25)).replace(tzinfo=timezone.utc)
        listed = self.walk(lambda **page: self.queries.scans(self.admin_context, start, narrow_end, outcome="REJECTED", **page), 1)
        self.assertEqual([value for value in listed if value in set(attempts)], [attempts[index] for index in (4, 3, 2, 1)])

    def test_employees_are_newest_registered_first_and_search_and_active_filters_page_correctly(self):
        tag = uuid4().hex[:8]

        def register(index):
            name = f"Order {tag} {'Even' if index % 2 == 0 else 'Odd'} {index}"
            return self.employees.register(
                self.admin_context, f"NF-{tag}-{index}", name, f"nf-{tag}-{index}@example.test", self.department_id,
                company_name="Example", phone="+1 202 555 0101",
            ).employee_id

        people = self.created(register)
        self.assert_newest_first(lambda **page: self.queries.list_employees(self.admin_context, **page), people)
        evens = self.walk(lambda **page: self.queries.list_employees(self.admin_context, q=f"{tag} Even", **page), 1)
        self.assertEqual(evens, [people[0], people[4], people[2]])
        for index in (1, 2, 4):
            self.employees.set_active(self.admin_context, people[index], False)
        inactive = self.walk(lambda **page: self.queries.list_employees(self.admin_context, active=False, **page), 1)
        self.assertEqual([value for value in inactive if value in set(people)], [people[4], people[2], people[1]])
        page = self.queries.list_employees(self.admin_context, q=tag, limit=2)
        with self.at(50):
            self.employees.archive_bulk(self.admin_context, [page["items"][-1]["id"]], "Removed in test")
        after = self.walk(lambda **next_page: self.queries.list_employees(
            self.admin_context, q=tag, **{**next_page, "before": next_page["before"] or page["next_cursor"]}))
        self.assertEqual(after, [people[3], people[2], people[1]])

    def test_visitor_master_qrs_staff_and_emails_are_newest_first(self):
        qrs = self.created(lambda _: self.qr.issue_master(self.admin_context).qr_id)
        self.assert_newest_first(lambda **page: self.queries.master_qrs(self.admin_context, **page), qrs)
        tag = uuid4().hex[:8]
        staff = self.created(lambda index: self.staff.create_staff(
            self.admin_context, f"Order Waiter {index}", f"order-{tag}-{index}@example.test", secrets.token_urlsafe(24), {"WAITER"}))
        self.assert_newest_first(lambda **page: self.queries.staff(self.admin_context, **page), staff)

        def email(index):
            registration = self.employees.register(
                self.admin_context, f"EM-{tag}-{index}", "Email Order", f"em-{tag}-{index}@example.test", self.department_id,
                company_name="Example", phone="+1 202 555 0101",
            )
            return registration.email_id

        emails = self.created(email)
        self.assert_newest_first(lambda **page: self.queries.email_status(self.admin_context, delivery_scope="all", **page), emails)

    def test_visitor_authorizations_are_newest_first(self):
        def authorize(_):
            credential = self.qr.issue_master(self.admin_context)
            return self.approvals.authorize(
                self.admin_context, uuid4(), credential.token, self.waiter_id, "TEST-SCANNER", self.meal_type_id, 1,
                "Fictional Visitor", visitor_organization="Example", visit_purpose="Order test",
            )

        authorizations = self.created(authorize)
        self.assert_newest_first(
            lambda **page: self.queries.visitor_authorizations(self.admin_context, status="all", **page), authorizations)

    def test_scanner_codes_are_newest_first(self):
        from meal_management.scanner_phones import ScannerPhoneService

        phones = ScannerPhoneService(self.db, secrets.token_bytes(32))
        codes = self.created(lambda index: phones.create_code(self.admin_context, f"Code {index}", date.today() + timedelta(days=30))["id"])
        with self.at(40):
            listed = [item["id"] for item in phones.list(self.admin_context)["codes"]]
        self.assertEqual([value for value in listed if value in set(codes)], [codes[index] for index in EXPECTED])
