import atexit
import ipaddress
import json
import os
import re
import ssl
import tempfile
from pathlib import Path

from .application import create_services
from .config import Settings
from .errors import ConfigurationError
from .runtime import RuntimeSettings


def _required(environment, name):
    value = environment.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError("MISSING_SETTING_" + name)
    return value.strip()


def scanner_networks(value):
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError("MISSING_SETTING_SCANNER_ALLOWED_CIDRS")
    try:
        entries = value.split(",")
        if len(entries) > 16:
            raise ValueError
        networks = tuple(ipaddress.ip_network(entry.strip(), strict=True) for entry in entries)
        if any(network.prefixlen == 0 or network.is_multicast or network.is_loopback or network.is_link_local for network in networks):
            raise ValueError
        return networks
    except ValueError:
        raise ConfigurationError("INVALID_SETTING_SCANNER_ALLOWED_CIDRS") from None


def _remove_certificate(path):
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


def vercel_configuration(application, environment):
    if application not in {"admin", "scanner"}:
        raise ConfigurationError("INVALID_APPLICATION")
    if environment.get("VERCEL") != "1":
        raise ConfigurationError("VERCEL_RUNTIME_REQUIRED")
    if environment.get("MEAL_APPLICATION", application) != application:
        raise ConfigurationError("VERCEL_APPLICATION_MISMATCH")
    values = dict(environment)
    if values.get("APP_ENV") != "production":
        raise ConfigurationError("VERCEL_REQUIRES_PRODUCTION_CONFIGURATION")
    values.setdefault("MAX_PHOTO_BYTES", "4000000")
    values.setdefault("EMAIL_PROCESS_LIMIT", "1")
    runtime = RuntimeSettings.from_env(values).for_application(application)
    runtime.for_application("admin")
    runtime.for_application("scanner")
    if runtime.email_auto_send_enabled:
        raise ConfigurationError("VERCEL_BACKGROUND_EMAIL_NOT_SUPPORTED")
    if runtime.email_process_limit != 1:
        raise ConfigurationError("VERCEL_EMAIL_PROCESS_LIMIT_MUST_BE_ONE")
    if runtime.max_photo_bytes > 4000000:
        raise ConfigurationError("VERCEL_PHOTO_LIMIT_EXCEEDED")
    if runtime.photo_backend != "vercel_blob" or runtime.email_backend != "gmail":
        raise ConfigurationError("VERCEL_REQUIRES_PRIVATE_BLOB_AND_GMAIL")
    networks = ()
    if application == "scanner" and runtime.scan_app_enabled and runtime.scanner_activation_secret is None:
        raise ConfigurationError("MISSING_SETTING_SCANNER_ACTIVATION_SECRET")
    certificate = _required(values, "DB_SSL_CA_PEM")
    if len(certificate) > 65536 or not certificate.startswith("-----BEGIN CERTIFICATE-----"):
        raise ConfigurationError("INVALID_SETTING_DB_SSL_CA_PEM")
    try:
        ssl.create_default_context(cadata=certificate)
    except (ssl.SSLError, ValueError):
        raise ConfigurationError("INVALID_SETTING_DB_SSL_CA_PEM") from None
    values["DB_SSL_CA"] = "/tmp/meal-vercel-ca-validation.pem"
    settings = Settings.from_env(values)
    if settings.db_user.lower() in {"root", "avnadmin"}:
        raise ConfigurationError("VERCEL_REQUIRES_RESTRICTED_DATABASE_ACCOUNT")
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*\.aivencloud\.com", settings.db_host):
        raise ConfigurationError("VERCEL_REQUIRES_AIVEN_DATABASE")
    descriptor, path = tempfile.mkstemp(prefix="meal-aiven-ca-", suffix=".pem")
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write(certificate + "\n")
        values["DB_SSL_CA"] = path
        settings = Settings.from_env(values)
    except Exception:
        _remove_certificate(path)
        raise
    atexit.register(_remove_certificate, path)
    return settings, runtime, networks


class VercelRequestMiddleware:
    def __init__(self, app, *, application, networks=()):
        self.app = app
        self.application = application
        self.networks = networks

    async def _reject(self, send, code, status):
        body = json.dumps({"error": {"code": code, "message": "This request is not permitted."}}).encode()
        await send({"type": "http.response.start", "status": status, "headers": [
            (b"content-type", b"application/json"), (b"cache-control", b"no-store"),
            (b"x-content-type-options", b"nosniff"),
        ]})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {}
        for name, value in scope.get("headers", []):
            key = name.lower()
            if key in {b"x-forwarded-proto", b"x-vercel-forwarded-for"}:
                if key in headers:
                    await self._reject(send, "INVALID_PROXY_HEADERS", 400)
                    return
                headers[key] = value
        if headers.get(b"x-forwarded-proto") != b"https":
            await self._reject(send, "HTTPS_REQUIRED", 400)
            return
        try:
            address = ipaddress.ip_address(headers[b"x-vercel-forwarded-for"].decode("ascii"))
        except (KeyError, ValueError, UnicodeError):
            await self._reject(send, "INVALID_PROXY_HEADERS", 400)
            return
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        forwarded = dict(scope, scheme="https", client=(str(address), 0))
        await self.app(forwarded, receive, send)


def create_vercel_app(application, env=None):
    settings, runtime, networks = vercel_configuration(application, os.environ if env is None else env)
    try:
        if application == "admin":
            from .admin_api import create_app
        else:
            from .scanner_api import create_app
        app = create_app(services=create_services(settings), runtime=runtime)
        app.add_middleware(VercelRequestMiddleware, application=application, networks=networks)
        return app
    except Exception:
        _remove_certificate(settings.db_ssl_ca)
        raise
