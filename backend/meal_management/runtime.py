import base64
import binascii
import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import urlsplit

from .errors import ConfigurationError


def _required(env, name):
    value = env.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError("MISSING_SETTING_" + name)
    return value.strip()


def _secret(env, name):
    value = _required(env, name)
    try:
        decoded = (
            base64.b64decode(value[7:], altchars=b"-_", validate=True)
            if value.startswith("base64:") else value.encode("utf-8")
        )
    except (ValueError, binascii.Error):
        raise ConfigurationError("INVALID_SETTING_" + name) from None
    if len(decoded) < 32 or value.lower().startswith(("replace", "change", "example", "placeholder")):
        raise ConfigurationError("INVALID_SETTING_" + name)
    return decoded


def _integer(env, name, default, minimum, maximum):
    try:
        value = int(env.get(name, str(default)))
    except (TypeError, ValueError):
        raise ConfigurationError("INVALID_SETTING_" + name) from None
    if not minimum <= value <= maximum:
        raise ConfigurationError("INVALID_SETTING_" + name)
    return value


def _boolean(env, name, default):
    value = env.get(name, str(default)).lower()
    if value not in {"true", "false", "1", "0"}:
        raise ConfigurationError("INVALID_SETTING_" + name)
    return value in {"true", "1"}


def _origin(value, name):
    try:
        parsed = urlsplit(value)
        port = parsed.port
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or any(character.isspace() for character in value)
            or port == 0
        ):
            raise ValueError
    except (ValueError, TypeError):
        raise ConfigurationError("INVALID_SETTING_" + name) from None
    hostname = parsed.hostname.lower()
    host_literal = "[" + hostname + "]" if ":" in hostname else hostname
    suffix = "" if port is None or (parsed.scheme, port) in {("http", 80), ("https", 443)} else ":" + str(port)
    return parsed.scheme + "://" + host_literal + suffix


@dataclass(frozen=True)
class RuntimeSettings:
    csrf_secret: bytes = field(repr=False)
    login_rate_secret: bytes = field(repr=False)
    environment: str = "development"
    app_origin: str = "http://localhost:8000"
    admin_origin: str | None = None
    scanner_origin: str = "http://localhost:8001"
    allowed_hosts: tuple[str, ...] = ("localhost",)
    cookie_secure: bool = False
    session_cookie: str = "meal_session"
    photo_backend: str = "local"
    photo_root: Path = Path("var/private/photos")
    email_backend: str = "preview"
    aws_region: str | None = None
    photo_bucket: str | None = None
    email_sender: str | None = None
    gmail_app_password: str | None = field(default=None, repr=False)
    email_send_enabled: bool = False
    email_auto_send_enabled: bool = False
    email_poll_seconds: int = 5
    email_batch_size: int = 10
    max_photo_bytes: int = 5 * 1024 * 1024
    session_idle_minutes: int = 30
    login_limit: int = 5
    login_ip_limit: int = 30
    login_window_seconds: int = 900
    scanner_enabled: bool | None = None
    scanner_request_limit: int = 120
    scanner_ip_limit: int = 600
    scanner_window_seconds: int = 60

    @property
    def scan_app_enabled(self):
        return self.environment == "development" if self.scanner_enabled is None else self.scanner_enabled

    def for_application(self, application):
        if application not in {"admin", "scanner"}:
            raise ConfigurationError("INVALID_APPLICATION")
        admin_origin = _origin(self.admin_origin or self.app_origin, "APP_ORIGIN")
        origin = admin_origin if application == "admin" else _origin(self.scanner_origin, "SCANNER_ORIGIN")
        if application == "scanner" and origin == admin_origin:
            raise ConfigurationError("SCANNER_ORIGIN_MUST_DIFFER_FROM_ADMIN_ORIGIN")
        parsed = urlsplit(origin)
        if self.environment == "production" and (parsed.scheme != "https" or not self.cookie_secure):
            raise ConfigurationError("PRODUCTION_REQUIRES_HTTPS_AND_SECURE_COOKIES")
        if parsed.scheme == "http" and self.cookie_secure:
            raise ConfigurationError("COOKIE_SECURE_REQUIRES_HTTPS_" + ("SCANNER_ORIGIN" if application == "scanner" else "APP_ORIGIN"))
        if parsed.hostname not in self.allowed_hosts:
            raise ConfigurationError("INVALID_SETTING_ALLOWED_HOSTS")
        return replace(self, app_origin=origin, admin_origin=admin_origin)

    @classmethod
    def from_env(cls, mapping=None):
        env = os.environ if mapping is None else mapping
        environment = env.get("APP_ENV", "development").strip().lower()
        if environment not in {"development", "production"}:
            raise ConfigurationError("INVALID_SETTING_APP_ENV")
        origin = _origin(env.get("APP_ORIGIN", "http://localhost:8000").strip(), "APP_ORIGIN")
        scanner_origin = _origin(env.get("SCANNER_ORIGIN", "http://localhost:8001").strip(), "SCANNER_ORIGIN")
        parsed = urlsplit(origin)
        hostname = parsed.hostname
        cookie_secure = _boolean(env, "COOKIE_SECURE", environment == "production")
        if environment == "production" and (parsed.scheme != "https" or not cookie_secure):
            raise ConfigurationError("PRODUCTION_REQUIRES_HTTPS_AND_SECURE_COOKIES")
        if parsed.scheme == "http" and cookie_secure:
            raise ConfigurationError("COOKIE_SECURE_REQUIRES_HTTPS_APP_ORIGIN")
        host_values = env.get("ALLOWED_HOSTS", hostname + "," + urlsplit(scanner_origin).hostname).split(",")
        hosts = tuple(dict.fromkeys(value.strip().lower() for value in host_values))
        if hostname not in hosts or any(
            not host or host == "*" or not re.fullmatch(r"[a-z0-9.:-]+", host)
            for host in hosts
        ):
            raise ConfigurationError("INVALID_SETTING_ALLOWED_HOSTS")
        photo_backend = env.get("PHOTO_BACKEND", "local").strip().lower()
        email_backend = env.get("EMAIL_BACKEND", "preview").strip().lower()
        if photo_backend not in {"local", "s3"}:
            raise ConfigurationError("INVALID_SETTING_PHOTO_BACKEND")
        if email_backend not in {"preview", "ses", "gmail"}:
            raise ConfigurationError("INVALID_SETTING_EMAIL_BACKEND")
        email_send_enabled = _boolean(env, "EMAIL_SEND_ENABLED", False)
        email_auto_send_enabled = _boolean(env, "EMAIL_AUTO_SEND_ENABLED", False)
        if email_auto_send_enabled and (not email_send_enabled or email_backend not in {"gmail", "ses"}):
            raise ConfigurationError("EMAIL_AUTO_SEND_REQUIRES_ENABLED_REAL_EMAIL")
        if environment == "production" and (photo_backend == "local" or email_backend == "preview"):
            raise ConfigurationError("PRODUCTION_REQUIRES_S3_STORAGE_AND_REAL_EMAIL")
        region = _required(env, "AWS_REGION") if photo_backend == "s3" or email_backend == "ses" else None
        if region and not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d", region):
            raise ConfigurationError("INVALID_SETTING_AWS_REGION")
        bucket = _required(env, "PHOTO_S3_BUCKET") if photo_backend == "s3" else None
        if bucket and not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
            raise ConfigurationError("INVALID_SETTING_PHOTO_S3_BUCKET")
        sender = _required(env, "EMAIL_SENDER") if email_backend in {"ses", "gmail"} else None
        if sender and not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", sender):
            raise ConfigurationError("INVALID_SETTING_EMAIL_SENDER")
        gmail_password = None
        if email_backend == "gmail":
            from .delivery import DeliveryError, validate_email_sender

            try:
                sender = validate_email_sender(sender)
            except DeliveryError:
                raise ConfigurationError("INVALID_SETTING_EMAIL_SENDER") from None
            gmail_password = _required(env, "GMAIL_APP_PASSWORD").replace(" ", "")
            if not re.fullmatch(r"[A-Za-z]{16}", gmail_password):
                raise ConfigurationError("INVALID_SETTING_GMAIL_APP_PASSWORD")
        return cls(
            csrf_secret=_secret(env, "APP_CSRF_SECRET"),
            login_rate_secret=_secret(env, "LOGIN_RATE_SECRET"),
            environment=environment,
            app_origin=origin,
            admin_origin=origin,
            scanner_origin=scanner_origin,
            allowed_hosts=hosts,
            cookie_secure=cookie_secure,
            session_cookie="__Host-meal_session" if cookie_secure else "meal_session",
            photo_backend=photo_backend,
            photo_root=Path(env.get("PRIVATE_PHOTO_ROOT", "var/private/photos")).absolute(),
            email_backend=email_backend,
            aws_region=region,
            photo_bucket=bucket,
            email_sender=sender,
            gmail_app_password=gmail_password,
            email_send_enabled=email_send_enabled,
            email_auto_send_enabled=email_auto_send_enabled,
            email_poll_seconds=_integer(env, "EMAIL_POLL_SECONDS", 5, 1, 300),
            email_batch_size=_integer(env, "EMAIL_BATCH_SIZE", 10, 1, 100),
            max_photo_bytes=_integer(env, "MAX_PHOTO_BYTES", 5 * 1024 * 1024, 1024, 20 * 1024 * 1024),
            session_idle_minutes=_integer(env, "SESSION_IDLE_MINUTES", 30, 1, 480),
            login_limit=_integer(env, "LOGIN_ATTEMPT_LIMIT", 5, 1, 100),
            login_ip_limit=_integer(env, "LOGIN_IP_ATTEMPT_LIMIT", 30, 1, 1000),
            login_window_seconds=_integer(env, "LOGIN_WINDOW_SECONDS", 900, 60, 86400),
            scanner_enabled=_boolean(env, "SCAN_APP_ENABLED", environment == "development"),
            scanner_request_limit=_integer(env, "SCAN_REQUEST_LIMIT", 120, 1, 10000),
            scanner_ip_limit=_integer(env, "SCAN_IP_LIMIT", 600, 1, 10000),
            scanner_window_seconds=_integer(env, "SCAN_WINDOW_SECONDS", 60, 1, 3600),
        )
