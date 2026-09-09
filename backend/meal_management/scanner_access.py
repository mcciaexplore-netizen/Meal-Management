import hashlib
import hmac
import re
import secrets
from datetime import timedelta

from .database import retry_transaction
from .errors import DomainError


class ScannerBrowser:
    def __init__(self, secret):
        self.secret = secret

    def _signature(self, purpose, value):
        return hmac.new(self.secret, (purpose + ":" + value).encode(), hashlib.sha256).hexdigest()

    def issue(self):
        nonce = secrets.token_urlsafe(32)
        return nonce + "." + self._signature("scanner-cookie", nonce)

    def valid(self, cookie):
        if not isinstance(cookie, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}\.[0-9a-f]{64}", cookie):
            return False
        nonce, signature = cookie.split(".")
        return hmac.compare_digest(signature, self._signature("scanner-cookie", nonce))

    def identity(self, cookie):
        if not self.valid(cookie):
            raise DomainError("SCANNER_BROWSER_REQUIRED")
        return bytes.fromhex(self._signature("scanner-browser", cookie))

    def csrf(self, cookie):
        if not self.valid(cookie):
            raise DomainError("SCANNER_BROWSER_REQUIRED")
        return self._signature("scanner-csrf", cookie)

    def verify_csrf(self, cookie, supplied):
        expected = self.csrf(cookie)
        if not isinstance(supplied, str) or not re.fullmatch(r"[0-9a-f]{64}", supplied) or not hmac.compare_digest(expected, supplied):
            raise DomainError("CSRF_REJECTED")


class ScannerLimiter:
    def __init__(self, database, settings):
        self.database = database
        self.settings = settings

    @retry_transaction
    def consume(self, browser_hash, address):
        if not isinstance(browser_hash, bytes) or len(browser_hash) != 32:
            raise DomainError("SCANNER_BROWSER_REQUIRED")
        targets = [
            (b"scanner-browser:" + browser_hash, self.settings.scanner_request_limit),
            (b"scanner-ip:" + str(address).encode(), self.settings.scanner_ip_limit),
        ]
        buckets = [
            (hmac.new(self.settings.login_rate_secret, key, hashlib.sha256).digest(), limit)
            for key, limit in targets
        ]
        denied = False
        with self.database.transaction() as tx:
            now = tx.now()
            for key, limit in sorted(buckets):
                tx.execute(
                    "INSERT INTO login_rate_limits (bucket_hash, window_started_at, attempts, updated_at) "
                    "VALUES (%s, %s, 0, %s) ON DUPLICATE KEY UPDATE bucket_hash = bucket_hash",
                    (key, now, now),
                )
                row = tx.one("SELECT * FROM login_rate_limits WHERE bucket_hash = %s FOR UPDATE", (key,))
                start, count = row["window_started_at"], row["attempts"]
                if now >= start + timedelta(seconds=self.settings.scanner_window_seconds):
                    start, count = now, 0
                blocked = count >= limit
                denied = denied or blocked
                tx.execute(
                    "UPDATE login_rate_limits SET window_started_at = %s, attempts = %s, blocked_until = %s, "
                    "updated_at = %s WHERE bucket_hash = %s",
                    (start, min(count + 1, limit + 1),
                     start + timedelta(seconds=self.settings.scanner_window_seconds) if blocked else None, now, key),
                )
        if denied:
            raise DomainError("SCANNER_RATE_LIMITED")
