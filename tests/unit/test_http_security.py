import copy
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta
from threading import RLock

from meal_management.errors import DomainError
from meal_management.http_security import DatabaseLoginLimiter, csrf_token
from meal_management.runtime import RuntimeSettings


class LimiterDatabase:
    def __init__(self):
        self.rows = {}
        self.accounts = {}
        self.clock = datetime(2026, 9, 9, 12)
        self.lock = RLock()
        self.commits = 0
        self.fail_commit = False

    @contextmanager
    def transaction(self):
        with self.lock:
            previous = copy.deepcopy(self.rows)
            try:
                yield self
                if self.fail_commit:
                    raise RuntimeError("Commit unavailable")
                self.commits += 1
            except BaseException:
                self.rows = previous
                raise

    def now(self):
        return self.clock

    def one(self, sql, params):
        if "FROM staff_accounts" in sql:
            staff_id = self.accounts.get(params[0])
            return {"id": staff_id} if staff_id else None
        return dict(self.rows[params[0]])

    def execute(self, sql, params):
        if sql.startswith("INSERT"):
            key, start, updated = params
            self.rows.setdefault(key, {"window_started_at": start, "attempts": 0, "blocked_until": None, "updated_at": updated})
        else:
            start, count, blocked, updated, key = params
            self.rows[key] = {"window_started_at": start, "attempts": count, "blocked_until": blocked, "updated_at": updated}


class LoginLimiterTests(unittest.TestCase):
    def setUp(self):
        self.db = LimiterDatabase()
        self.settings = RuntimeSettings(csrf_secret=b"c" * 32, login_rate_secret=b"r" * 32, login_limit=2, login_ip_limit=3)
        self.limiter = DatabaseLoginLimiter(self.db, self.settings)

    def test_account_limit_is_shared_across_ips_and_normalized(self):
        self.limiter.consume(" A@EXAMPLE.COM ", "192.0.2.1")
        self.limiter.consume("a@example.com", "192.0.2.2")
        with self.assertRaises(DomainError) as caught:
            self.limiter.consume("a@example.com", "192.0.2.3")
        self.assertEqual(caught.exception.code, "RATE_LIMITED")
        self.assertEqual(self.db.commits, 3)
        self.assertNotIn("a@example.com", repr(self.db.rows))
        self.assertNotIn("192.0.2.", repr(self.db.rows))

    def test_mysql_equivalent_account_aliases_share_staff_bucket(self):
        self.db.accounts = {"jose@example.com": 7, "josé@example.com": 7}
        self.limiter.consume("jose@example.com", "192.0.2.1")
        self.limiter.consume("josé@example.com", "192.0.2.2")
        with self.assertRaises(DomainError):
            self.limiter.consume("jose@example.com", "192.0.2.3")

    def test_ip_limit_applies_across_accounts(self):
        for index in range(3):
            self.limiter.consume(str(index) + "@example.com", "192.0.2.1")
        with self.assertRaises(DomainError):
            self.limiter.consume("next@example.com", "192.0.2.1")

    def test_expired_window_resets_without_extending_on_rejection(self):
        self.limiter.consume("a@example.com", "192.0.2.1")
        self.limiter.consume("a@example.com", "192.0.2.1")
        with self.assertRaises(DomainError):
            self.limiter.consume("a@example.com", "192.0.2.1")
        self.db.clock += timedelta(seconds=self.settings.login_window_seconds)
        self.limiter.consume("a@example.com", "192.0.2.1")
        self.assertEqual({row["attempts"] for row in self.db.rows.values()}, {1})

    def test_commit_failure_never_grants_attempt(self):
        self.db.fail_commit = True
        with self.assertRaises(RuntimeError):
            self.limiter.consume("a@example.com", "192.0.2.1")
        self.assertEqual(self.db.rows, {})

    def test_parallel_callers_observe_committed_limits_with_transaction_fake(self):
        def consume(index):
            try:
                self.limiter.consume("a@example.com", "192.0.2." + str(index))
                return True
            except DomainError:
                return False

        with ThreadPoolExecutor(max_workers=6) as executor:
            allowed = list(executor.map(consume, range(6)))
        self.assertEqual(sum(allowed), 2)
        self.assertEqual(self.db.commits, 6)

    def test_csrf_changes_with_session_seed_or_secret(self):
        original = csrf_token(b"a" * 32, "seed", "session")
        self.assertEqual(len(original), 64)
        alternatives = {
            csrf_token(b"b" * 32, "seed", "session"),
            csrf_token(b"a" * 32, "different", "session"),
            csrf_token(b"a" * 32, "seed", "different"),
        }
        self.assertNotIn(original, alternatives)


if __name__ == "__main__":
    unittest.main()
