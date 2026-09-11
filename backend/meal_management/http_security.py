import asyncio
import hashlib
import hmac
import json
from datetime import timedelta
from uuid import uuid4

from .database import retry_transaction
from .errors import DomainError


def csrf_token(secret, seed, session=""):
    return hmac.new(secret, (seed + ":" + session).encode(), hashlib.sha256).hexdigest()


def error_body(code, request_id=None):
    messages = {
        "INVALID_CREDENTIALS": "Email or password is incorrect.",
        "AUTHENTICATION_REQUIRED": "Please sign in.",
        "SESSION_IDLE_EXPIRED": "Your session expired. Please sign in again.",
        "ROLE_REQUIRED": "You do not have permission to do this.",
        "RATE_LIMITED": "Too many login attempts. Please try again later.",
        "INVALID_INPUT": "Check the supplied values and try again.",
        "CSRF_REJECTED": "Refresh the page and try again.",
        "ORIGIN_REJECTED": "This request did not come from the application.",
        "SERVICE_UNAVAILABLE": "The service is temporarily unavailable. Please try again.",
        "PROCESSING_UNCONFIRMED": "The serving result is unconfirmed. Retry the same request.",
        "SCAN_RECEIPT_UNCONFIRMED": "The scan could not be confirmed. Retry the same request.",
        "REQUEST_TOO_LARGE": "The uploaded content is too large.",
        "NOT_FOUND": "The requested item was not found.",
        "SCANNER_BROWSER_REQUIRED": "Refresh the scanner to continue. Keep any pending serving unchanged.",
        "SCANNER_ACTIVATION_REQUIRED": "Activate this scanner device to continue.",
        "SCANNER_ACTIVATION_INVALID": "The scanner activation code is incorrect.",
        "SCANNER_NOT_CONFIGURED": "The scanner setup is incomplete. Ask the office administrator to finish setup.",
        "SCANNER_DISABLED": "The meal scanner is currently unavailable.",
        "SCANNER_RATE_LIMITED": "Too many scanner requests. Wait briefly, then retry the same serving.",
        "REAL_EMAIL_NOT_AUTHORIZED": "Email sending is disabled. An administrator must enable the configured email provider before sending.",
        "EMAIL_APPROVAL_REQUIRED": "Approve this bulk email before sending it.",
        "EMAIL_DELIVERY_NEEDS_REVIEW": "The email result is uncertain. Check its delivery status before creating another send.",
        "EMAIL_CLAIM_UNCONFIRMED": "The send could not be confirmed. Check the same email's status before retrying.",
    }
    return {"error": {"code": code, "message": messages.get(code, code.replace("_", " ").capitalize() + "."), "request_id": request_id}}


def status_for(code):
    if code in {"AUTHENTICATION_REQUIRED", "INVALID_CREDENTIALS", "STAFF_INACTIVE", "SESSION_IDLE_EXPIRED", "SCANNER_BROWSER_REQUIRED", "SCANNER_ACTIVATION_REQUIRED", "SCANNER_ACTIVATION_INVALID"}:
        return 401
    if code in {"ROLE_REQUIRED", "FORBIDDEN", "CSRF_REJECTED", "ORIGIN_REJECTED"}:
        return 403
    if code in {"RATE_LIMITED", "SCANNER_RATE_LIMITED"}:
        return 429
    if code in {"SERVICE_UNAVAILABLE", "PROCESSING_UNCONFIRMED", "SCAN_RECEIPT_UNCONFIRMED", "TRANSACTION_RETRY_REQUIRED", "DATABASE_NOT_READY", "SCANNER_NOT_CONFIGURED"}:
        return 503
    if code == "SCANNER_DISABLED":
        return 404
    if code.endswith("NOT_FOUND") or code == "NOT_FOUND":
        return 404
    if "EXISTS" in code or "ALREADY" in code or "IDEMPOTENCY" in code:
        return 409
    return 400


class DatabaseLoginLimiter:
    def __init__(self, database, settings):
        self.database = database
        self.settings = settings

    @retry_transaction
    def consume(self, email, address):
        account = str(email).strip().casefold()
        denied = False
        with self.database.transaction() as tx:
            staff = tx.one("SELECT id FROM staff_accounts WHERE email = %s", (account,))
            account_key = "staff:" + str(staff["id"]) if staff else "account:" + account
            targets = [
                (hmac.new(self.settings.login_rate_secret, account_key.encode(), hashlib.sha256).digest(), self.settings.login_limit),
                (hmac.new(self.settings.login_rate_secret, ("ip:" + address).encode(), hashlib.sha256).digest(), self.settings.login_ip_limit),
            ]
            now = tx.now()
            for key, limit in sorted(targets):
                tx.execute(
                    "INSERT INTO login_rate_limits (bucket_hash, window_started_at, attempts, updated_at) "
                    "VALUES (%s, %s, 0, %s) ON DUPLICATE KEY UPDATE bucket_hash = bucket_hash", (key, now, now),
                )
                row = tx.one("SELECT * FROM login_rate_limits WHERE bucket_hash = %s FOR UPDATE", (key,))
                start = row["window_started_at"]
                count = row["attempts"]
                if now >= start + timedelta(seconds=self.settings.login_window_seconds):
                    start, count = now, 0
                blocked = count >= limit
                denied = denied or blocked
                tx.execute(
                    "UPDATE login_rate_limits SET window_started_at = %s, attempts = %s, blocked_until = %s, updated_at = %s WHERE bucket_hash = %s",
                    (start, min(count + 1, limit + 1), start + timedelta(seconds=self.settings.login_window_seconds) if blocked else None, now, key),
                )
        if denied:
            raise DomainError("RATE_LIMITED")


class SecurityMiddleware:
    def __init__(self, app, settings):
        self.app = app
        self.settings = settings

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        scope.setdefault("state", {})["request_id"] = uuid4().hex
        headers = {key.decode("latin1").lower(): value.decode("latin1") for key, value in scope["headers"]}
        path = scope["path"]
        sensitive = path.startswith("/api/") or path.startswith("/health/")
        response_started = False
        response_finished = False

        async def secured_send(message):
            nonlocal response_started, response_finished
            if message["type"] == "http.response.start":
                response_started = True
                extra = [
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"x-frame-options", b"SAMEORIGIN"),
                    (b"permissions-policy", b"camera=(self), microphone=(), geolocation=()"),
                    (b"x-request-id", scope["state"]["request_id"].encode()),
                    (b"content-security-policy", b"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; connect-src 'self'; media-src 'self' blob:; object-src 'none'; base-uri 'none'; frame-ancestors 'self'; form-action 'self'; frame-src 'self' blob:"),
                ]
                if sensitive:
                    extra.append((b"cache-control", b"no-store"))
                if self.settings.environment == "production":
                    extra.append((b"strict-transport-security", b"max-age=31536000"))
                message["headers"] = list(message.get("headers", [])) + extra
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                response_finished = True
            await send(message)

        async def fail(code, status):
            body = json.dumps(error_body(code, scope["state"]["request_id"])).encode()
            await secured_send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json")]})
            await secured_send({"type": "http.response.body", "body": body})

        if self.settings.environment == "production" and scope.get("scheme") != "https":
            await fail("HTTPS_REQUIRED", 400)
            return
        method = scope["method"]
        if method not in {"GET", "HEAD", "OPTIONS"} and path.startswith("/api/"):
            if headers.get("origin") != self.settings.app_origin:
                await fail("ORIGIN_REJECTED", 403)
                return
            maximum = self.settings.max_photo_bytes if path.endswith("/photo") else 65536
            try:
                declared = int(headers.get("content-length", "0"))
                if declared < 0 or declared > maximum:
                    await fail("REQUEST_TOO_LARGE", 413)
                    return
                chunks = []
                length = 0
                while True:
                    message = await asyncio.wait_for(receive(), timeout=15)
                    if message["type"] == "http.disconnect":
                        return
                    chunk = message.get("body", b"")
                    length += len(chunk)
                    if length > maximum:
                        await fail("REQUEST_TOO_LARGE", 413)
                        return
                    chunks.append(chunk)
                    if not message.get("more_body", False):
                        break
                body = b"".join(chunks)
            except (ValueError, asyncio.TimeoutError):
                await fail("INVALID_INPUT", 400)
                return
            used = False

            async def replay_receive():
                nonlocal used
                if not used:
                    used = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await receive()

            app_receive = replay_receive
        else:
            app_receive = receive
        try:
            await self.app(scope, app_receive, secured_send)
        except Exception:
            if not response_started:
                await fail("SERVICE_UNAVAILABLE", 503)
            elif not response_finished:
                await secured_send({"type": "http.response.body", "body": b"", "more_body": False})
