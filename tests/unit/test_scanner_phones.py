import hashlib
import json
import secrets
import unittest
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from unittest.mock import Mock, patch

from meal_management import scanner_phones
from meal_management.errors import DomainError
from meal_management.models import Actor
from meal_management.scanner_phones import (
    CODE_ALPHABET, ScannerPhoneService, active_until_for, add_months, display_code, end_of_ist_day,
    generate_code, ist_day_of, ist_today, latest_active_day, normalize_code,
)


class CodeFormatTests(unittest.TestCase):
    def test_generated_codes_use_eight_unambiguous_uppercase_characters(self):
        codes = {generate_code() for _ in range(500)}
        self.assertGreater(len(codes), 495)
        for code in codes:
            self.assertEqual(len(code), 8)
            self.assertTrue(set(code) <= set(CODE_ALPHABET))
        self.assertEqual(set(CODE_ALPHABET) & set("0O1I"), set())
        self.assertEqual(len(set(CODE_ALPHABET)), 32)

    def test_normalization_accepts_spoken_and_typed_variants(self):
        for value in ("ABCD-EFGH", "abcd-efgh", " abcd efgh ", "ABCDEFGH", "ab-cd-ef-gh"):
            with self.subTest(value=value):
                self.assertEqual(normalize_code(value), "ABCDEFGH")
        self.assertEqual(display_code("ABCDEFGH"), "ABCD-EFGH")

    def test_normalization_rejects_ambiguous_wrong_length_and_non_text_values(self):
        for value in ("ABCD-EFG0", "ABCD-EFGO", "ABCD-EFG1", "ABCD-EFGI", "ABCDEFG", "ABCDEFGHJ",
                      "ABCD_EFGH", "ABCD.EFGH", "", None, 12345678, "A" * 40,
                      "fictional-device-activation-code"):
            with self.subTest(value=value):
                self.assertIsNone(normalize_code(value))


class IndiaTimeTests(unittest.TestCase):
    def test_active_until_is_the_end_of_the_chosen_day_in_india_stored_as_utc(self):
        now = datetime(2026, 10, 25, 10, 0)
        self.assertEqual(active_until_for(date(2026, 10, 25), now), datetime(2026, 10, 25, 18, 30))
        self.assertEqual(active_until_for(date(2026, 10, 30), now), datetime(2026, 10, 30, 18, 30))
        self.assertEqual(ist_day_of(datetime(2026, 10, 30, 18, 30)), date(2026, 10, 30))

    def test_india_midnight_boundary_decides_which_day_is_today(self):
        before_midnight = datetime(2026, 10, 25, 18, 29, 59, 999999)
        at_midnight = datetime(2026, 10, 25, 18, 30)
        self.assertEqual(ist_today(before_midnight), date(2026, 10, 25))
        self.assertEqual(ist_today(at_midnight), date(2026, 10, 26))
        self.assertEqual(active_until_for(date(2026, 10, 25), before_midnight), at_midnight)
        with self.assertRaisesRegex(DomainError, "ACTIVE_UNTIL_OUT_OF_RANGE"):
            active_until_for(date(2026, 10, 25), at_midnight)
        self.assertEqual(active_until_for(date(2026, 10, 26), at_midnight), datetime(2026, 10, 26, 18, 30))

    def test_utc_date_differs_from_india_date_after_india_midnight(self):
        now = datetime(2026, 12, 31, 20, 0)
        self.assertEqual(now.date(), date(2026, 12, 31))
        self.assertEqual(ist_today(now), date(2027, 1, 1))
        self.assertEqual(end_of_ist_day(date(2027, 1, 1)), datetime(2027, 1, 1, 18, 30))

    def test_active_until_is_capped_at_six_calendar_months(self):
        cases = (
            (datetime(2026, 10, 25, 10), date(2027, 4, 25)),
            (datetime(2026, 8, 31, 10), date(2027, 2, 28)),
            (datetime(2027, 8, 31, 10), date(2028, 2, 29)),
            (datetime(2026, 12, 31, 20), date(2027, 7, 1)),
        )
        for now, latest in cases:
            with self.subTest(now=now):
                self.assertEqual(latest_active_day(now), latest)
                active_until_for(latest, now)
                with self.assertRaisesRegex(DomainError, "ACTIVE_UNTIL_OUT_OF_RANGE"):
                    active_until_for(latest + timedelta(days=1), now)
        self.assertEqual(add_months(date(2026, 1, 31), 1), date(2026, 2, 28))

    def test_active_until_requires_a_plain_date(self):
        now = datetime(2026, 10, 25, 10)
        for value in (datetime(2026, 10, 26), "2026-10-26", None, 20261026):
            with self.subTest(value=value), self.assertRaisesRegex(DomainError, "INVALID_ACTIVE_UNTIL"):
                active_until_for(value, now)


class FakeTransaction:
    def __init__(self, now, rows=()):
        self.clock = now
        self.queue = list(rows)
        self.statements = []
        self.inserts = []

    def now(self):
        return self.clock

    def one(self, sql, params=()):
        self.statements.append((sql, params))
        if "FROM scan_app_settings" in sql:
            return {"staff_id": 9}
        return self.queue.pop(0) if self.queue else None

    def all(self, sql, params=()):
        self.statements.append((sql, params))
        return []

    def execute(self, sql, params=()):
        self.statements.append((sql, params))

    def insert(self, sql, params=()):
        self.inserts.append((sql, params))
        return 41


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.secret = secrets.token_bytes(32)
        self.now = datetime(2026, 10, 25, 10)
        self.tx = FakeTransaction(self.now)
        self.database = Mock()

        @contextmanager
        def transaction():
            yield self.tx

        self.database.transaction.side_effect = transaction
        self.service = ScannerPhoneService(self.database, self.secret)
        self.actor = patch.object(scanner_phones, "require_actor", return_value=Actor(1, frozenset({"ADMIN"})))
        self.actor.start()
        self.addCleanup(self.actor.stop)

    def audits(self):
        return [params for sql, params in self.tx.inserts if sql.startswith("INSERT INTO audit_events")]

    def test_code_hash_is_keyed_and_never_a_plain_digest(self):
        code = "ABCDEFGH"
        digest = self.service.code_hash(code)
        self.assertEqual(len(digest), 32)
        self.assertNotEqual(digest, hashlib.sha256(code.encode()).digest())
        self.assertNotEqual(digest, ScannerPhoneService(Mock(), secrets.token_bytes(32)).code_hash(code))
        self.assertEqual(digest, ScannerPhoneService(Mock(), self.secret).code_hash(code))
        with self.assertRaisesRegex(DomainError, "INVALID_SCANNER_CODE_SECRET"):
            ScannerPhoneService(Mock(), b"short")

    def test_created_code_is_returned_once_and_only_its_hash_is_stored_or_audited(self):
        result = self.service.create_code(object(), "  Counter 2  ", date(2026, 11, 30))
        code = normalize_code(result["code"])
        self.assertRegex(result["code"], r"^[A-Z2-9]{4}-[A-Z2-9]{4}$")
        self.assertEqual(result["label"], "Counter 2")
        self.assertEqual(result["code_expires_at"], self.now + timedelta(hours=24))
        self.assertEqual(result["active_until"], datetime(2026, 11, 30, 18, 30))
        self.assertEqual(result["active_until_date"], "2026-11-30")
        phone = next(params for sql, params in self.tx.inserts if sql.startswith("INSERT INTO scanner_phones"))
        self.assertIn(self.service.code_hash(code), phone)
        stored = repr(self.tx.inserts) + repr(self.tx.statements)
        self.assertNotIn(code, stored)
        self.assertNotIn(result["code"], stored)
        audit = self.audits()[0]
        self.assertEqual(audit[1], "SCANNER_CODE_CREATED")
        self.assertNotIn(code, audit[5])
        self.assertEqual(json.loads(audit[5])["label"], "Counter 2")

    def test_code_creation_validates_label_and_date_before_storing(self):
        for label, day, error in (("", date(2026, 11, 1), "INVALID_SCANNER_LABEL"),
                                  ("x" * 101, date(2026, 11, 1), "INVALID_SCANNER_LABEL"),
                                  ("Counter", date(2027, 4, 26), "ACTIVE_UNTIL_OUT_OF_RANGE"),
                                  ("Counter", date(2026, 10, 24), "ACTIVE_UNTIL_OUT_OF_RANGE")):
            with self.subTest(label=label, day=day), self.assertRaisesRegex(DomainError, error):
                self.service.create_code(object(), label, day)
        self.assertEqual(self.tx.inserts, [])

    def test_activation_rejects_malformed_codes_without_database_access(self):
        for value in ("short", "ABCD-EFG0", None):
            with self.subTest(value=value), self.assertRaisesRegex(DomainError, "SCANNER_ACTIVATION_INVALID"):
                self.service.activate_code(value, secrets.token_bytes(32))
        self.database.transaction.assert_not_called()

    def test_activation_rejects_used_revoked_and_expired_codes_with_one_error(self):
        base = {"id": 5, "label": "Counter", "source": "CODE", "activated_at": None, "revoked_at": None,
                "code_expires_at": self.now + timedelta(hours=1), "active_until": self.now + timedelta(days=3)}
        variants = (
            None,
            {**base, "activated_at": self.now - timedelta(minutes=5)},
            {**base, "revoked_at": self.now - timedelta(minutes=5)},
            {**base, "code_expires_at": self.now},
            {**base, "active_until": self.now},
            {**base, "source": "SECRET"},
        )
        for row in variants:
            self.tx.queue = [row]
            with self.subTest(row=row), self.assertRaisesRegex(DomainError, "SCANNER_ACTIVATION_INVALID"):
                self.service.activate_code("ABCD-EFGH", secrets.token_bytes(32))
        self.assertFalse(any(sql.startswith("UPDATE") for sql, _ in self.tx.statements))
        self.tx.queue = [base]
        browser = secrets.token_bytes(32)
        self.assertEqual(self.service.activate_code("abcd efgh", browser), {"id": 5, "active_until": base["active_until"]})
        update = [params for sql, params in self.tx.statements if sql.startswith("UPDATE scanner_phones")]
        self.assertEqual(update, [(browser, self.now, 5)])
        self.assertEqual(self.audits()[-1][1], "SCANNER_PHONE_ACTIVATED")
        self.assertEqual(self.audits()[-1][0], 9)

    def test_authorize_rejects_revoked_and_expired_phones(self):
        browser = secrets.token_bytes(32)
        for row in ({"id": 3, "active_until": self.now, "revoked_at": None},
                    {"id": 3, "active_until": None, "revoked_at": self.now - timedelta(seconds=1)},
                    {"id": 3, "active_until": self.now + timedelta(days=1), "revoked_at": self.now}):
            self.tx.queue = [row]
            with self.subTest(row=row), self.assertRaisesRegex(DomainError, "SCANNER_ACTIVATION_REQUIRED"):
                self.service.authorize(browser, adopt_legacy=True)
        self.tx.queue = [{"id": 3, "active_until": self.now + timedelta(seconds=1), "revoked_at": None}]
        self.assertEqual(self.service.authorize(browser)["id"], 3)

    def test_unknown_signed_cookie_is_adopted_only_while_the_fallback_is_configured(self):
        browser = secrets.token_bytes(32)
        with self.assertRaisesRegex(DomainError, "SCANNER_ACTIVATION_REQUIRED"):
            self.service.authorize(browser, adopt_legacy=False)
        self.assertEqual(self.tx.inserts, [])
        self.assertEqual(self.service.authorize(browser, adopt_legacy=True), {"id": 41, "active_until": None})
        phone = next(params for sql, params in self.tx.inserts if sql.startswith("INSERT INTO scanner_phones"))
        self.assertEqual(phone, ("Activated before codes", browser, self.now, self.now))
        self.assertEqual(self.audits()[-1][1], "SCANNER_PHONE_REGISTERED")

    def test_secret_activation_expires_after_thirty_days(self):
        result = self.service.register_secret_activation(secrets.token_bytes(32))
        self.assertEqual(result["active_until"], self.now + timedelta(days=30))
        phone = next(params for sql, params in self.tx.inserts if sql.startswith("INSERT INTO scanner_phones"))
        self.assertEqual(phone[0], "Emergency code")

    def test_revoke_distinguishes_phones_from_unused_codes(self):
        for activated, action in ((self.now, "SCANNER_PHONE_REVOKED"), (None, "SCANNER_CODE_CANCELLED")):
            self.tx.queue = [{"id": 6, "label": "Counter", "source": "CODE", "activated_at": activated, "revoked_at": None}]
            self.assertEqual(self.service.revoke(object(), 6), {"id": 6, "revoked": True})
            self.assertEqual(self.audits()[-1][1], action)
        self.tx.queue = [{"id": 6, "label": "Counter", "source": "CODE", "activated_at": None, "revoked_at": self.now}]
        with self.assertRaisesRegex(DomainError, "SCANNER_PHONE_ALREADY_REVOKED"):
            self.service.revoke(object(), 6)
        self.tx.queue = [None]
        with self.assertRaisesRegex(DomainError, "SCANNER_PHONE_NOT_FOUND"):
            self.service.revoke(object(), 6)
        for value in (0, -1, "6", True, 2**64):
            with self.subTest(value=value), self.assertRaisesRegex(DomainError, "INVALID_SCANNER_PHONE_ID"):
                self.service.revoke(object(), value)

    def test_service_requires_an_administrator(self):
        self.actor.stop()
        with patch.object(scanner_phones, "require_actor", side_effect=DomainError("ROLE_REQUIRED")):
            for call in (lambda: self.service.list(object()),
                         lambda: self.service.create_code(object(), "Counter", date(2026, 11, 1)),
                         lambda: self.service.revoke(object(), 1)):
                with self.assertRaisesRegex(DomainError, "ROLE_REQUIRED"):
                    call()
        self.actor.start()
        self.assertEqual(self.tx.inserts, [])
