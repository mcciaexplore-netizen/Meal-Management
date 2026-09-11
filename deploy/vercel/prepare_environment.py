import argparse
import hashlib
import hmac
import io
import ipaddress
import json
import os
import re
import ssl
import stat
import sys
from pathlib import Path
from urllib.parse import urlsplit


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from dotenv import dotenv_values
from dotenv.parser import parse_stream

from meal_management.config import Settings
from meal_management.delivery import validate_email_sender
from meal_management.errors import DomainError
from meal_management.runtime import RuntimeSettings


SENSITIVE_KEYS = frozenset({
    "DB_PASSWORD", "QR_ENCRYPTION_KEYS", "APP_CSRF_SECRET", "LOGIN_RATE_SECRET", "GMAIL_APP_PASSWORD",
    "SCANNER_ACTIVATION_SECRET",
})
OMITTED_KEYS = frozenset({"BLOB_READ_WRITE_TOKEN", "BLOB_STORE_ID", "BLOB_WEBHOOK_PUBLIC_KEY", "WEBHOOK_SECRET"})
PROJECTS = {"admin": "mccia-meal-admin", "scanner": "mccia-meal-scanner"}


def _safe_path(value):
    path = Path(os.path.abspath(Path(value).expanduser()))
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise DomainError("VERCEL_ENV_REFUSES_SYMLINKS")
    return path


def _read_file(path, *, secret=True, limit=262144):
    path = _safe_path(path)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            attributes = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(attributes.st_mode) or attributes.st_nlink != 1
                or attributes.st_uid != os.getuid()
                or (stat.S_IMODE(attributes.st_mode) != 0o600 if secret else attributes.st_mode & 0o022)
            ):
                raise DomainError("VERCEL_ENV_PRIVATE_OWNED_INPUT_REQUIRED")
            data = source.read(limit + 1)
    except OSError:
        raise DomainError("VERCEL_ENV_INPUT_UNAVAILABLE") from None
    if not data or len(data) > limit:
        raise DomainError("VERCEL_ENV_INVALID_INPUT_SIZE")
    try:
        return data.decode("utf-8")
    except UnicodeError:
        raise DomainError("VERCEL_ENV_INVALID_INPUT_ENCODING") from None


def _environment(path):
    content = _read_file(path)
    bindings = list(parse_stream(io.StringIO(content)))
    keys = []
    for binding in bindings:
        if binding.error:
            raise DomainError("VERCEL_ENV_INVALID_ENVIRONMENT_FILE")
        if binding.key is not None:
            if not re.fullmatch(r"[A-Z][A-Z0-9_]*", binding.key) or binding.value is None:
                raise DomainError("VERCEL_ENV_INVALID_ENVIRONMENT_FILE")
            keys.append(binding.key)
    if len(keys) != len(set(keys)):
        raise DomainError("VERCEL_ENV_DUPLICATE_ENVIRONMENT_KEY")
    return dict(dotenv_values(stream=io.StringIO(content), interpolate=False))


def _matches(first, second):
    return hmac.compare_digest(first.encode("utf-8"), second.encode("utf-8"))


def _certificate(aiven, candidate):
    if not aiven.db_ssl_ca or not candidate.db_ssl_ca:
        raise DomainError("VERCEL_ENV_VERIFIED_CA_REQUIRED")
    first = Path(aiven.db_ssl_ca)
    second = Path(candidate.db_ssl_ca)
    if not first.is_absolute() or not second.is_absolute() or _safe_path(first) != _safe_path(second):
        raise DomainError("VERCEL_ENV_CA_CONFIGURATION_MISMATCH")
    certificate = _read_file(first, secret=False, limit=65536)
    if not certificate.startswith("-----BEGIN CERTIFICATE-----"):
        raise DomainError("VERCEL_ENV_INVALID_CA_CERTIFICATE")
    try:
        ssl.create_default_context(cadata=certificate)
    except (ssl.SSLError, ValueError):
        raise DomainError("VERCEL_ENV_INVALID_CA_CERTIFICATE") from None
    return certificate


def _output_path(value):
    output = _safe_path(value)
    if output.exists():
        raise DomainError("VERCEL_ENV_OUTPUT_ALREADY_EXISTS")
    try:
        parent = output.parent.stat()
    except OSError:
        raise DomainError("VERCEL_ENV_PRIVATE_PARENT_REQUIRED") from None
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700:
        raise DomainError("VERCEL_ENV_PRIVATE_PARENT_REQUIRED")
    return output


def _validate_sources(local_values, aiven_values, runtime_values):
    local = Settings.from_env(local_values)
    aiven = Settings.from_env(aiven_values)
    local_runtime = RuntimeSettings.from_env(local_values)
    aiven_runtime = RuntimeSettings.from_env(aiven_values)
    if (
        not hmac.compare_digest(local_runtime.csrf_secret, aiven_runtime.csrf_secret)
        or not hmac.compare_digest(local_runtime.login_rate_secret, aiven_runtime.login_rate_secret)
        or not _matches(",".join(local.qr_encryption_keys), ",".join(aiven.qr_encryption_keys))
    ):
        raise DomainError("VERCEL_ENV_EXISTING_APPLICATION_KEYS_MUST_MATCH")
    candidate = Settings.from_env(dict(runtime_values, QR_ENCRYPTION_KEYS=",".join(aiven.qr_encryption_keys)))
    if (
        aiven.db_name != "defaultdb" or candidate.db_name != "defaultdb"
        or candidate.db_user != "meal_runtime"
        or not re.fullmatch(r"[a-z0-9][a-z0-9.-]*\.aivencloud\.com", candidate.db_host)
        or (candidate.db_host, candidate.db_port, candidate.db_name) != (aiven.db_host, aiven.db_port, aiven.db_name)
        or _matches(candidate.db_password, aiven.db_password)
    ):
        raise DomainError("VERCEL_ENV_RESTRICTED_DATABASE_CONFIGURATION_MISMATCH")
    if local_runtime.email_backend != "gmail":
        raise DomainError("VERCEL_ENV_LOCAL_GMAIL_CONFIGURATION_REQUIRED")
    sender = aiven_values.get("EMAIL_SENDER")
    if sender and not _matches(validate_email_sender(sender), local_runtime.email_sender):
        raise DomainError("VERCEL_ENV_GMAIL_CONFIGURATION_MISMATCH")
    password = aiven_values.get("GMAIL_APP_PASSWORD")
    if password and not _matches(password.strip().replace(" ", ""), local_runtime.gmail_app_password):
        raise DomainError("VERCEL_ENV_GMAIL_CONFIGURATION_MISMATCH")
    return aiven, candidate, local_runtime


def _shared_values(aiven, candidate, local_runtime, aiven_values, certificate, *, blob_host, admin_origin, scanner_origin):
    values = {
        "APP_ENV": "production", "APP_ORIGIN": admin_origin, "SCANNER_ORIGIN": scanner_origin,
        "COOKIE_SECURE": "true", "APP_CSRF_SECRET": aiven_values["APP_CSRF_SECRET"],
        "LOGIN_RATE_SECRET": aiven_values["LOGIN_RATE_SECRET"], "LOGIN_WINDOW_SECONDS": "60",
        "DB_HOST": candidate.db_host, "DB_PORT": str(candidate.db_port), "DB_NAME": candidate.db_name,
        "DB_USER": candidate.db_user, "DB_PASSWORD": candidate.db_password,
        "DB_CONNECT_TIMEOUT": str(candidate.db_connect_timeout), "DB_SSL_CA_PEM": certificate,
        "QR_ENCRYPTION_KEYS": ",".join(aiven.qr_encryption_keys),
        "PHOTO_BACKEND": "vercel_blob", "BLOB_STORE_HOST": blob_host,
        "MAX_PHOTO_BYTES": "4000000", "EMAIL_BACKEND": "gmail",
        "EMAIL_SENDER": local_runtime.email_sender, "GMAIL_APP_PASSWORD": local_runtime.gmail_app_password,
        "EMAIL_SEND_ENABLED": "false", "EMAIL_AUTO_SEND_ENABLED": "false", "EMAIL_PROCESS_LIMIT": "1",
        "SCAN_APP_ENABLED": "false", "SCANNER_ACTIVATION_SECRET": "", "SCANNER_ALLOWED_CIDRS": "", "SCAN_REQUEST_LIMIT": "120",
        "SCAN_IP_LIMIT": "600", "SCAN_WINDOW_SECONDS": "60",
    }
    validation = dict(values, BLOB_READ_WRITE_TOKEN="validation-only-not-a-real-blob-credential-00000000")
    runtime = RuntimeSettings.from_env(validation)
    runtime.for_application("admin")
    runtime.for_application("scanner")
    if runtime.app_origin == runtime.scanner_origin:
        raise DomainError("SCANNER_ORIGIN_MUST_DIFFER_FROM_ADMIN_ORIGIN")
    hosts = (urlsplit(runtime.app_origin).hostname, urlsplit(runtime.scanner_origin).hostname)
    if hosts[0] == hosts[1] or any(
        not hostname or "." not in hostname or ":" in hostname
        for hostname in hosts
    ) or any(urlsplit(origin).port is not None for origin in (runtime.app_origin, runtime.scanner_origin)):
        raise DomainError("VERCEL_ENV_PUBLIC_HTTPS_ORIGINS_REQUIRED")
    for hostname in hosts:
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            continue
        raise DomainError("VERCEL_ENV_PUBLIC_HTTPS_ORIGINS_REQUIRED")
    values.update({
        "APP_ORIGIN": runtime.app_origin, "SCANNER_ORIGIN": runtime.scanner_origin,
        "ALLOWED_HOSTS": ",".join(dict.fromkeys(hosts)), "BLOB_STORE_HOST": runtime.blob_store_host,
    })
    RuntimeSettings.from_env(dict(values, BLOB_READ_WRITE_TOKEN=validation["BLOB_READ_WRITE_TOKEN"]))
    allowed = {
        binding.key for binding in parse_stream(io.StringIO(Path(__file__).with_name("environment.example").read_text(encoding="utf-8")))
        if binding.key is not None
    } - OMITTED_KEYS
    if set(values) != allowed:
        raise DomainError("VERCEL_ENV_ALLOWLIST_MISMATCH")
    return values


def _encode(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _write(path, data):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())


def prepare_environment(*, local_env, aiven_env, runtime_env, output_directory, blob_host, admin_origin, scanner_origin,
                        admin_project_id=None, scanner_project_id=None):
    paths = {"local_env": _safe_path(local_env), "aiven_env": _safe_path(aiven_env), "runtime_env": _safe_path(runtime_env)}
    if len(set(paths.values())) != 3 or paths["runtime_env"].name != "database-runtime.env":
        raise DomainError("VERCEL_ENV_DISTINCT_VERIFIED_SOURCE_FILES_REQUIRED")
    pending = paths["runtime_env"].with_suffix(".pending.env")
    if pending.exists() or pending.is_symlink():
        raise DomainError("VERCEL_ENV_RUNTIME_CREDENTIALS_REQUIRE_REVIEW")
    project_ids = {"admin": admin_project_id, "scanner": scanner_project_id}
    if any(value is not None for value in project_ids.values()) and (
        any(not isinstance(value, str) or not re.fullmatch(r"prj_[A-Za-z0-9]{10,64}", value) for value in project_ids.values())
        or admin_project_id == scanner_project_id
    ):
        raise DomainError("VERCEL_ENV_DISTINCT_PROJECT_IDS_REQUIRED")
    output = _output_path(output_directory)
    local_values = _environment(paths["local_env"])
    aiven_values = _environment(paths["aiven_env"])
    runtime_values = _environment(paths["runtime_env"])
    aiven, candidate, local_runtime = _validate_sources(local_values, aiven_values, runtime_values)
    certificate = _certificate(aiven, candidate)
    shared = _shared_values(
        aiven, candidate, local_runtime, aiven_values, certificate,
        blob_host=blob_host, admin_origin=admin_origin, scanner_origin=scanner_origin,
    )
    payloads = {}
    for application in PROJECTS:
        values = dict(shared, MEAL_APPLICATION=application)
        payloads[application + ".env.json"] = _encode([
            {"key": key, "value": value, "type": "sensitive" if key in SENSITIVE_KEYS else "encrypted", "target": ["production"]}
            for key, value in sorted(values.items())
        ])
    review = {
        "format_version": 1, "status": "prepared_offline", "scope": "mccias-projects",
        "projects": {
            application: {
                "name": name, "id": project_ids[application],
                "domains": [urlsplit(shared["APP_ORIGIN" if application == "admin" else "SCANNER_ORIGIN"]).hostname],
                "payload_file": application + ".env.json",
                "sha256": hashlib.sha256(payloads[application + ".env.json"]).hexdigest(),
            }
            for application, name in PROJECTS.items()
        },
        "origins": {"admin": shared["APP_ORIGIN"], "scanner": shared["SCANNER_ORIGIN"]},
        "database": {"host": candidate.db_host, "port": candidate.db_port, "name": candidate.db_name, "user": candidate.db_user},
        "blob_store_host": shared["BLOB_STORE_HOST"], "blob_token_source": "existing_vercel_connection",
        "blob_token_validation": "not_performed", "provider_validation": "not_performed",
        "deployment_performed": False, "sending_enabled": False, "scanner_enabled": False,
        "input_files": {name: str(path) for name, path in paths.items()},
        "certificate_file": str(_safe_path(candidate.db_ssl_ca)),
        "review_file": str(output / "review.json"),
    }
    payloads["review.json"] = _encode(review)
    try:
        output.mkdir(mode=0o700)
        for name, content in payloads.items():
            _write(output / name, content)
        descriptor = os.open(output, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        raise DomainError("VERCEL_ENV_WRITE_FAILED_PRIVATE_DIRECTORY_PRESERVED") from None
    return review


class PreparationParser(argparse.ArgumentParser):
    def error(self, message):
        raise DomainError("VERCEL_ENV_INVALID_ARGUMENTS")


def main(argv=None):
    parser = PreparationParser(description="Prepare private production Vercel environment payloads offline without uploading or validating provider access.")
    parser.add_argument("--local-env", required=True, type=Path, help="Explicit owned 0600 local environment file containing the current Gmail configuration.")
    parser.add_argument("--aiven-env", required=True, type=Path, help="Explicit owned 0600 Aiven environment file preserving application keys.")
    parser.add_argument("--runtime-env", required=True, type=Path, help="Verified owned 0600 database-runtime.env created by the runtime-account helper.")
    parser.add_argument("--blob-host", required=True, help="Verified private Blob hostname without scheme or path; no token argument is accepted.")
    parser.add_argument("--admin-origin", required=True, help="Stable administrator HTTPS origin.")
    parser.add_argument("--scanner-origin", required=True, help="Distinct stable scanner HTTPS origin.")
    parser.add_argument("--admin-project-id", help="Verified administrator Vercel project ID; provide both project IDs for upload review.")
    parser.add_argument("--scanner-project-id", help="Verified scanner Vercel project ID; provide both project IDs for upload review.")
    parser.add_argument("--output", required=True, type=Path, help="New output directory under an existing owned 0700 parent directory.")
    try:
        arguments = parser.parse_args(argv)
        review = prepare_environment(
            local_env=arguments.local_env, aiven_env=arguments.aiven_env, runtime_env=arguments.runtime_env,
            output_directory=arguments.output, blob_host=arguments.blob_host,
            admin_origin=arguments.admin_origin, scanner_origin=arguments.scanner_origin,
            admin_project_id=arguments.admin_project_id, scanner_project_id=arguments.scanner_project_id,
        )
    except DomainError as error:
        print(error.code, file=sys.stderr)
        return 1
    except Exception:
        print("VERCEL_ENV_PREPARATION_FAILED", file=sys.stderr)
        return 1
    print("Private production payloads prepared offline. Review: " + review["review_file"])
    print("The Blob token is supplied by the existing Vercel connection and was not validated locally. No provider access or upload occurred.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
