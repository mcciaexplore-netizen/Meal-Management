import re
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, timedelta
from unittest.mock import patch

from test_mysql_services import MealServicesFixture


class ScannerPhoneMySQLTests(MealServicesFixture):
    def setUp(self):
        from meal_management.scanner_phones import ScannerPhoneService

        super().setUp()
        self.service = ScannerPhoneService(self.db, secrets.token_bytes(32))

    def now(self):
        return self.scalar("SELECT UTC_TIMESTAMP(6)")

    def day(self, days=30):
        from meal_management.scanner_phones import ist_today

        return ist_today(self.now()) + timedelta(days=days)

    def phone(self, phone_id):
        return self.rows("SELECT * FROM scanner_phones WHERE id = %s", (phone_id,))[0]

    def audit_actions(self, phone_id):
        return [row["action"] for row in self.rows(
            "SELECT action FROM audit_events WHERE entity_type = 'scanner_phones' AND entity_id = %s ORDER BY id",
            (str(phone_id),),
        )]

    def insert_code_row(self, code, expires_offset, until_offset):
        now = self.now()
        cursor = self.admin_connection.cursor()
        try:
            cursor.execute(
                "INSERT INTO scanner_phones (label, source, code_hash, code_expires_at, active_until, "
                "created_by_staff_id, created_at) VALUES ('Fixture code', 'CODE', %s, %s, %s, %s, %s)",
                (self.service.code_hash(code), now + expires_offset, now + until_offset,
                 self.scalar("SELECT id FROM staff_accounts WHERE email = 'admin@example.test'"), now),
            )
            return cursor.lastrowid
        finally:
            cursor.close()

    def statement_error(self, sql, params=()):
        cursor = self.admin_connection.cursor()
        try:
            cursor.execute(sql, params)
        except Exception as error:
            return getattr(error, "errno", None)
        finally:
            cursor.close()
        return None

    def test_full_lifecycle_creates_activates_authorizes_and_revokes_with_audit(self):
        from meal_management.errors import DomainError
        from meal_management.scanner_phones import end_of_ist_day, normalize_code

        created = self.service.create_code(self.admin_context, "Counter 2", self.day(10))
        code = normalize_code(created["code"])
        row = self.phone(created["id"])
        self.assertEqual(bytes(row["code_hash"]), self.service.code_hash(code))
        self.assertEqual(row["active_until"], end_of_ist_day(self.day(10)))
        self.assertLessEqual(abs((row["code_expires_at"] - row["created_at"]) - timedelta(hours=24)), timedelta(seconds=1))
        listing = self.service.list(self.admin_context)
        self.assertIn(created["id"], [item["id"] for item in listing["codes"]])
        self.assertNotIn(created["id"], [item["id"] for item in listing["phones"]])
        browser = secrets.token_bytes(32)
        activated = self.service.activate_code(created["code"].lower(), browser)
        self.assertEqual(activated["id"], created["id"])
        self.assertEqual(self.service.authorize(browser)["id"], created["id"])
        listing = self.service.list(self.admin_context)
        phone = next(item for item in listing["phones"] if item["id"] == created["id"])
        self.assertEqual(phone["active_until_date"], self.day(10).isoformat())
        self.assertNotIn("code", phone)
        self.assertNotIn(created["id"], [item["id"] for item in listing["codes"]])
        with self.assertRaisesRegex(DomainError, "SCANNER_ACTIVATION_INVALID"):
            self.service.activate_code(code, secrets.token_bytes(32))
        self.service.revoke(self.admin_context, created["id"])
        with self.assertRaisesRegex(DomainError, "SCANNER_ACTIVATION_REQUIRED"):
            self.service.authorize(browser, adopt_legacy=True)
        self.assertNotIn(created["id"], [item["id"] for item in self.service.list(self.admin_context)["phones"]])
        self.assertEqual(self.audit_actions(created["id"]), [
            "SCANNER_CODE_CREATED", "SCANNER_PHONE_ACTIVATED", "SCANNER_PHONE_REVOKED",
        ])
        audit_text = repr(self.rows("SELECT before_data, after_data FROM audit_events WHERE entity_type = 'scanner_phones'"))
        self.assertNotIn(code, audit_text)
        self.assertNotIn(created["code"], audit_text)

    def test_simultaneous_activations_of_one_code_bind_exactly_one_phone(self):
        from meal_management.errors import DomainError

        created = self.service.create_code(self.admin_context, "Shared code", self.day(5))
        barrier = threading.Barrier(4)
        browsers = [secrets.token_bytes(32) for _ in range(4)]

        def activate(browser):
            barrier.wait(timeout=10)
            try:
                self.service.activate_code(created["code"], browser)
                return browser
            except DomainError as error:
                self.assertEqual(error.code, "SCANNER_ACTIVATION_INVALID")
                return None

        with ThreadPoolExecutor(max_workers=4) as pool:
            winners = [result for result in pool.map(activate, browsers) if result is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(bytes(self.phone(created["id"])["browser_hash"]), winners[0])
        self.assertEqual(self.audit_actions(created["id"]).count("SCANNER_PHONE_ACTIVATED"), 1)

    def test_unused_code_expires_after_twenty_four_hours_and_phone_after_active_until(self):
        from meal_management.errors import DomainError

        expired = self.insert_code_row("EXPRDCDE", timedelta(seconds=-1), timedelta(days=5))
        with self.assertRaisesRegex(DomainError, "SCANNER_ACTIVATION_INVALID"):
            self.service.activate_code("EXPR-DCDE", secrets.token_bytes(32))
        self.assertIsNone(self.phone(expired)["activated_at"])
        self.assertNotIn(expired, [item["id"] for item in self.service.list(self.admin_context)["codes"]])
        lapsing = self.insert_code_row("LAPSNGPH", timedelta(hours=1), timedelta(seconds=3))
        browser = secrets.token_bytes(32)
        self.service.activate_code("LAPSNGPH", browser)
        self.service.authorize(browser)
        threading.Event().wait(3.5)
        with self.assertRaisesRegex(DomainError, "SCANNER_ACTIVATION_REQUIRED"):
            self.service.authorize(browser, adopt_legacy=True)
        self.assertNotIn(lapsing, [item["id"] for item in self.service.list(self.admin_context)["phones"]])

    def test_unused_code_can_be_cancelled_and_then_never_activates(self):
        from meal_management.errors import DomainError

        created = self.service.create_code(self.admin_context, "Cancelled", self.day(5))
        self.service.revoke(self.admin_context, created["id"])
        with self.assertRaisesRegex(DomainError, "SCANNER_ACTIVATION_INVALID"):
            self.service.activate_code(created["code"], secrets.token_bytes(32))
        with self.assertRaisesRegex(DomainError, "SCANNER_PHONE_ALREADY_REVOKED"):
            self.service.revoke(self.admin_context, created["id"])
        self.assertEqual(self.audit_actions(created["id"]), ["SCANNER_CODE_CREATED", "SCANNER_CODE_CANCELLED"])
        errno = self.statement_error(
            "UPDATE scanner_phones SET browser_hash = %s, activated_at = UTC_TIMESTAMP(6) WHERE id = %s",
            (secrets.token_bytes(32), created["id"]),
        )
        self.assertEqual(errno, 1644)

    def test_legacy_cookie_is_adopted_once_even_under_concurrency_and_secret_phones_expire(self):
        from meal_management.errors import DomainError

        browser = secrets.token_bytes(32)
        with self.assertRaisesRegex(DomainError, "SCANNER_ACTIVATION_REQUIRED"):
            self.service.authorize(browser, adopt_legacy=False)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM scanner_phones WHERE browser_hash = %s", (browser,)), 0)
        barrier = threading.Barrier(3)

        def adopt(_):
            barrier.wait(timeout=10)
            return self.service.authorize(browser, adopt_legacy=True)["id"]

        with ThreadPoolExecutor(max_workers=3) as pool:
            identifiers = set(pool.map(adopt, range(3)))
        self.assertEqual(len(identifiers), 1)
        row = self.phone(identifiers.pop())
        self.assertEqual((row["source"], row["label"], row["active_until"]), ("LEGACY", "Activated before codes", None))
        self.assertEqual(self.audit_actions(row["id"]), ["SCANNER_PHONE_REGISTERED"])
        self.service.revoke(self.admin_context, row["id"])
        with self.assertRaisesRegex(DomainError, "SCANNER_ACTIVATION_REQUIRED"):
            self.service.authorize(browser, adopt_legacy=True)
        secret_phone = self.service.register_secret_activation(secrets.token_bytes(32))
        stored = self.phone(secret_phone["id"])
        self.assertEqual(stored["source"], "SECRET")
        self.assertLessEqual(abs((stored["active_until"] - stored["activated_at"]) - timedelta(days=30)), timedelta(seconds=1))

    def test_database_blocks_deletes_identity_changes_and_unrevoking(self):
        created = self.service.create_code(self.admin_context, "Guarded", self.day(5))
        browser = secrets.token_bytes(32)
        self.service.activate_code(created["code"], browser)
        identifier = created["id"]
        for sql, params in (
            ("DELETE FROM scanner_phones WHERE id = %s", (identifier,)),
            ("UPDATE scanner_phones SET label = 'Renamed' WHERE id = %s", (identifier,)),
            ("UPDATE scanner_phones SET active_until = active_until + INTERVAL 1 DAY WHERE id = %s", (identifier,)),
            ("UPDATE scanner_phones SET browser_hash = %s WHERE id = %s", (secrets.token_bytes(32), identifier)),
            ("UPDATE scanner_phones SET code_hash = %s WHERE id = %s", (secrets.token_bytes(32), identifier)),
        ):
            with self.subTest(sql=sql):
                self.assertEqual(self.statement_error(sql, params), 1644)
        self.service.revoke(self.admin_context, identifier)
        self.assertEqual(self.statement_error(
            "UPDATE scanner_phones SET revoked_at = NULL, revoked_by_staff_id = NULL WHERE id = %s", (identifier,),
        ), 1644)
        self.assertEqual(self.statement_error(
            "INSERT INTO scanner_phones (label, source, active_until, created_at) VALUES ('Bad', 'CODE', UTC_TIMESTAMP(6), UTC_TIMESTAMP(6))",
        ), 3819)
        self.assertEqual(self.statement_error(
            "INSERT INTO scanner_phones (label, source, browser_hash, created_at) VALUES ('Bad', 'LEGACY', %s, UTC_TIMESTAMP(6))",
            (secrets.token_bytes(32),),
        ), 3819)

    def test_active_until_respects_the_india_day_and_six_month_cap(self):
        from meal_management.errors import DomainError
        from meal_management.scanner_phones import end_of_ist_day, ist_today, latest_active_day

        now = self.now()
        latest = latest_active_day(now)
        created = self.service.create_code(self.admin_context, "Longest", latest)
        self.assertEqual(self.phone(created["id"])["active_until"], end_of_ist_day(latest))
        stored = self.phone(created["id"])["active_until"]
        self.assertEqual((stored.hour, stored.minute), (18, 30))
        today = self.service.create_code(self.admin_context, "Today", ist_today(now))
        self.assertGreater(self.phone(today["id"])["active_until"], now)
        for day in (latest + timedelta(days=1), ist_today(now) - timedelta(days=1)):
            with self.subTest(day=day), self.assertRaisesRegex(DomainError, "ACTIVE_UNTIL_OUT_OF_RANGE"):
                self.service.create_code(self.admin_context, "Out of range", day)

    def test_waiters_cannot_manage_scanner_phones(self):
        from meal_management.errors import DomainError

        for call in (lambda: self.service.list(self.waiter_context),
                     lambda: self.service.create_code(self.waiter_context, "Counter", self.day(5)),
                     lambda: self.service.revoke(self.waiter_context, 1)):
            with self.assertRaises(DomainError):
                call()

    def test_lifecycle_only_uses_privileges_granted_to_meal_runtime(self):
        from meal_management import database
        from meal_management.runtime_account import INSERT_TABLES, UPDATE_TABLES

        seen = []
        lock = threading.Lock()
        originals = {name: getattr(database.Session, name) for name in ("one", "all", "execute", "insert")}

        def recorder(name):
            def wrapped(session, sql, params=()):
                with lock:
                    seen.append(sql)
                return originals[name](session, sql, params)
            return wrapped

        with patch.multiple(database.Session, **{name: recorder(name) for name in originals}):
            created = self.service.create_code(self.admin_context, "Privileges", self.day(5))
            browser = secrets.token_bytes(32)
            self.service.activate_code(created["code"], browser)
            self.service.authorize(browser)
            self.service.authorize(secrets.token_bytes(32), adopt_legacy=True)
            self.service.register_secret_activation(secrets.token_bytes(32))
            self.service.list(self.admin_context)
            self.service.revoke(self.admin_context, created["id"])
        writes = set()
        for sql in seen:
            statement = sql.strip().upper()
            self.assertFalse(statement.startswith(("DELETE", "DROP", "ALTER", "CREATE", "GRANT", "TRUNCATE")), sql)
            match = re.match(r"(INSERT INTO|UPDATE)\s+([a-z_]+)", sql.strip(), re.IGNORECASE)
            if match:
                writes.add((match[1].split()[0].upper(), match[2]))
        self.assertIn(("INSERT", "scanner_phones"), writes)
        self.assertIn(("UPDATE", "scanner_phones"), writes)
        for verb, table in writes:
            allowed = INSERT_TABLES if verb == "INSERT" else UPDATE_TABLES
            self.assertIn(table, allowed, (verb, table))
