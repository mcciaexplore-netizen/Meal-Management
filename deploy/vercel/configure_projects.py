import argparse
import hashlib
import json
import os
import re
import ssl
import stat
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from meal_management.config import Settings
from meal_management.errors import DomainError
from meal_management.runtime import RuntimeSettings


SCOPE = "mccias-projects"
PROJECTS = {"admin": "mccia-meal-admin", "scanner": "mccia-meal-scanner"}
CONFIRMATION = "CONFIGURE mccias-projects/mccia-meal-admin AND mccias-projects/mccia-meal-scanner PRODUCTION"
VERCEL_CLI = PROJECT_ROOT / "build/vercel-tools/node_modules/.bin/vercel"
SENSITIVE_KEYS = frozenset({"DB_PASSWORD", "QR_ENCRYPTION_KEYS", "APP_CSRF_SECRET", "LOGIN_RATE_SECRET", "GMAIL_APP_PASSWORD"})
BLOB_TYPES = {"BLOB_READ_WRITE_TOKEN": "sensitive", "BLOB_STORE_ID": "plain", "BLOB_WEBHOOK_PUBLIC_KEY": "plain"}
FORCED_VALUES = {
    "APP_ENV": "production", "COOKIE_SECURE": "true", "DB_NAME": "defaultdb", "DB_USER": "meal_runtime",
    "PHOTO_BACKEND": "vercel_blob", "MAX_PHOTO_BYTES": "4000000", "EMAIL_BACKEND": "gmail",
    "EMAIL_SEND_ENABLED": "false", "EMAIL_AUTO_SEND_ENABLED": "false", "EMAIL_PROCESS_LIMIT": "1",
    "SCAN_APP_ENABLED": "false", "SCANNER_ALLOWED_CIDRS": "",
}


class ConfigurationError(Exception):
    def __init__(self, code, *, changes_possible=False, verified=()):
        self.code = code
        self.changes_possible = changes_possible
        self.verified = tuple(verified)
        super().__init__(code)


def _fail(code):
    raise ConfigurationError(code)


def _path(value):
    path = Path(os.path.abspath(Path(value).expanduser()))
    if any(item.is_symlink() for item in (path, *path.parents)):
        _fail("VERCEL_SETTINGS_UNSAFE_PATH")
    return path


def _private_bytes(path, limit=1048576):
    path = _path(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        attributes = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(attributes.st_mode) or attributes.st_nlink != 1
            or attributes.st_uid != os.getuid() or stat.S_IMODE(attributes.st_mode) != 0o600
        ):
            _fail("VERCEL_SETTINGS_PRIVATE_FILE_REQUIRED")
        value = source.read(limit + 1)
        if not value or len(value) > limit:
            _fail("VERCEL_SETTINGS_INVALID_FILE_SIZE")
        return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("VERCEL_SETTINGS_DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _json(data):
    try:
        return json.loads(data, object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, TypeError):
        _fail("VERCEL_SETTINGS_INVALID_JSON")


def _allowed_keys():
    source = Path(__file__).with_name("environment.example").read_text(encoding="utf-8")
    keys = {line.split("=", 1)[0] for line in source.splitlines() if line.strip()}
    if any(not re.fullmatch(r"[A-Z][A-Z0-9_]+", key) for key in keys):
        _fail("VERCEL_SETTINGS_INVALID_TEMPLATE")
    return keys - {"BLOB_READ_WRITE_TOKEN"} | {"MEAL_APPLICATION"}


def _payload(rows, role, review):
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
        _fail("VERCEL_SETTINGS_INVALID_PAYLOAD")
    values = {}
    allowed = _allowed_keys()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"key", "value", "type", "target"}:
            _fail("VERCEL_SETTINGS_INVALID_PAYLOAD")
        key = row["key"]
        if not isinstance(key, str) or key in values or key not in allowed:
            _fail("VERCEL_SETTINGS_PAYLOAD_KEYS_MISMATCH")
        if not isinstance(row["value"], str) or len(row["value"]) > 65536 or "\x00" in row["value"]:
            _fail("VERCEL_SETTINGS_INVALID_VALUE")
        if not row["value"] and key != "SCANNER_ALLOWED_CIDRS":
            _fail("VERCEL_SETTINGS_EMPTY_VALUE")
        if row["type"] != ("sensitive" if key in SENSITIVE_KEYS else "encrypted") or row["target"] != ["production"]:
            _fail("VERCEL_SETTINGS_INVALID_TYPE_OR_TARGET")
        values[key] = row["value"]
    if set(values) != allowed or any(values.get(key) != value for key, value in FORCED_VALUES.items()):
        _fail("VERCEL_SETTINGS_PAYLOAD_KEYS_OR_SAFETY_FLAGS_MISMATCH")
    if values["MEAL_APPLICATION"] != role:
        _fail("VERCEL_SETTINGS_APPLICATION_MISMATCH")
    settings = Settings.from_env(values)
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*\.aivencloud\.com", settings.db_host):
        _fail("VERCEL_SETTINGS_AIVEN_DATABASE_REQUIRED")
    if review.get("database") != {"host": settings.db_host, "port": settings.db_port, "name": settings.db_name, "user": settings.db_user}:
        _fail("VERCEL_SETTINGS_REVIEW_DATABASE_MISMATCH")
    runtime = RuntimeSettings.from_env(dict(values, BLOB_READ_WRITE_TOKEN="validation-only-not-a-real-blob-credential-00000000"))
    runtime.for_application("admin")
    runtime.for_application("scanner")
    origins = {"admin": runtime.app_origin, "scanner": runtime.scanner_origin}
    if review.get("origins") != origins or review.get("blob_store_host") != runtime.blob_store_host:
        _fail("VERCEL_SETTINGS_REVIEW_ORIGINS_MISMATCH")
    hosts = []
    for application, origin in origins.items():
        parsed = urlsplit(origin)
        if parsed.scheme != "https" or parsed.port is not None or parsed.hostname != review["projects"][application]["domains"][0]:
            _fail("VERCEL_SETTINGS_VERIFIED_HTTPS_ORIGINS_REQUIRED")
        hosts.append(parsed.hostname)
    if len(set(hosts)) != 2 or set(runtime.allowed_hosts) != set(hosts):
        _fail("VERCEL_SETTINGS_HOSTS_MISMATCH")
    certificate = values["DB_SSL_CA_PEM"]
    if not certificate.startswith("-----BEGIN CERTIFICATE-----"):
        _fail("VERCEL_SETTINGS_VALID_DATABASE_CA_REQUIRED")
    try:
        ssl.create_default_context(cadata=certificate)
    except (ValueError, ssl.SSLError):
        _fail("VERCEL_SETTINGS_VALID_DATABASE_CA_REQUIRED")
    return values


def load_configuration(directory):
    root = _path(directory)
    metadata = root.stat()
    if not root.is_dir() or metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
        _fail("VERCEL_SETTINGS_PRIVATE_DIRECTORY_REQUIRED")
    if {path.name for path in root.iterdir()} != {"admin.env.json", "scanner.env.json", "review.json"}:
        _fail("VERCEL_SETTINGS_DIRECTORY_CONTENTS_MISMATCH")
    review_bytes = _private_bytes(root / "review.json")
    review = _json(review_bytes)
    if (
        not isinstance(review, dict) or type(review.get("format_version")) is not int or review["format_version"] != 1
        or review.get("status") != "prepared_offline" or review.get("scope") != SCOPE
        or not isinstance(review.get("projects"), dict) or set(review["projects"]) != set(PROJECTS)
        or any(review.get(key) is not False for key in ("deployment_performed", "sending_enabled", "scanner_enabled"))
        or review.get("blob_token_source") != "existing_vercel_connection"
    ):
        _fail("VERCEL_SETTINGS_INVALID_REVIEW")
    payloads = {}
    hashes = {"review.json": hashlib.sha256(review_bytes).hexdigest()}
    configurations = {}
    ids = set()
    for role, name in PROJECTS.items():
        project = review["projects"][role]
        if (
            not isinstance(project, dict) or project.get("name") != name
            or not isinstance(project.get("id"), str) or not re.fullmatch(r"prj_[A-Za-z0-9]{10,64}", project["id"])
            or project["id"] in ids or project.get("payload_file") != role + ".env.json"
            or not isinstance(project.get("domains"), list) or len(project["domains"]) != 1
            or not isinstance(project["domains"][0], str) or not re.fullmatch(r"[a-z0-9][a-z0-9.-]*\.[a-z]{2,}", project["domains"][0])
            or not isinstance(project.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", project["sha256"])
        ):
            _fail("VERCEL_SETTINGS_REVIEW_PROJECT_MISMATCH")
        ids.add(project["id"])
    for role in PROJECTS:
        project = review["projects"][role]
        data = _private_bytes(root / project["payload_file"])
        if hashlib.sha256(data).hexdigest() != project["sha256"]:
            _fail("VERCEL_SETTINGS_PAYLOAD_CHECKSUM_MISMATCH")
        rows = _json(data)
        configurations[role] = _payload(rows, role, review)
        payloads[role] = rows
        hashes[project["payload_file"]] = project["sha256"]
    shared = [{key: value for key, value in configurations[role].items() if key != "MEAL_APPLICATION"} for role in PROJECTS]
    if shared[0] != shared[1]:
        _fail("VERCEL_SETTINGS_SHARED_CONFIGURATION_MISMATCH")
    return root, review, payloads, hashes


def _request(cli, directory, endpoint, *, method="GET", payload=None, payload_sha256=None):
    arguments = [str(cli), "api", endpoint, "--method", method]
    input_options = {"stdin": subprocess.DEVNULL}
    if payload is not None:
        data = _private_bytes(payload)
        if hashlib.sha256(data).hexdigest() != payload_sha256:
            _fail("VERCEL_SETTINGS_PREPARED_FILES_CHANGED")
        input_options = {"input": json.dumps(data.decode("utf-8")).encode("utf-8")}
        arguments.extend(["--input", "-", "--header", "Content-Type: application/json"])
    arguments.extend(["--scope", SCOPE, "--raw"])
    environment = {key: os.environ[key] for key in ("PATH", "HOME", "XDG_CONFIG_HOME") if key in os.environ}
    environment.update({"CI": "1", "NO_COLOR": "1", "VERCEL_TELEMETRY_DISABLED": "1"})
    try:
        result = subprocess.run(
            arguments, cwd=directory, env=environment, **input_options,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, check=False,
        )
    except subprocess.TimeoutExpired:
        _fail("VERCEL_SETTINGS_PROVIDER_TIMEOUT")
    except OSError:
        _fail("VERCEL_SETTINGS_CLI_UNAVAILABLE")
    if result.returncode != 0:
        _fail("VERCEL_SETTINGS_PROVIDER_REQUEST_FAILED")
    if not isinstance(result.stdout, bytes) or len(result.stdout) > 2097152:
        _fail("VERCEL_SETTINGS_PROVIDER_RESPONSE_INVALID")
    response = _json(result.stdout)
    if not isinstance(response, dict) or response.get("error"):
        _fail("VERCEL_SETTINGS_PROVIDER_RESPONSE_INVALID")
    return response


def _unpaged(response):
    pagination = response.get("pagination")
    if pagination is not None and (not isinstance(pagination, dict) or pagination.get("next") is not None):
        _fail("VERCEL_SETTINGS_PAGINATION_REQUIRES_REVIEW")


def _metadata(response, project):
    _unpaged(response)
    rows = response.get("envs")
    if not isinstance(rows, list) or len(rows) > 100:
        _fail("VERCEL_SETTINGS_ENVIRONMENT_METADATA_INVALID")
    result = {}
    for row in rows:
        if (
            not isinstance(row, dict) or not isinstance(row.get("key"), str) or row["key"] in result
            or row.get("target") != ["production"] or row.get("gitBranch") is not None
            or row.get("customEnvironmentIds") not in (None, []) or row.get("configurationId") is not None
            or row.get("visibility") not in (None, "secret" if row.get("type") == "sensitive" else "config")
            or row.get("projectId", project["id"]) != project["id"]
            or row.get("type") not in {"sensitive", "plain", "encrypted"}
        ):
            _fail("VERCEL_SETTINGS_ENVIRONMENT_METADATA_INVALID")
        result[row["key"]] = {"key": row["key"], "type": row["type"], "target": row["target"]}
    return result


def _domains(response, project):
    _unpaged(response)
    rows = response.get("domains")
    if not isinstance(rows, list) or len(rows) != len(project["domains"]):
        _fail("VERCEL_SETTINGS_PROJECT_DOMAINS_MISMATCH")
    names = set()
    for row in rows:
        if (
            not isinstance(row, dict) or row.get("projectId") != project["id"]
            or row.get("name") not in project["domains"] or row.get("name") in names
            or row.get("verified") is not True or row.get("redirect") is not None
            or row.get("gitBranch") is not None or row.get("customEnvironmentId") is not None
        ):
            _fail("VERCEL_SETTINGS_PROJECT_DOMAINS_MISMATCH")
        names.add(row["name"])


def _expected_metadata(rows):
    return {row["key"]: {key: row[key] for key in ("key", "type", "target")} for row in rows}


def _created(response, expected, project):
    if response.get("failed") != [] or not isinstance(response.get("created"), list):
        _fail("VERCEL_SETTINGS_CREATION_PARTIAL_OR_UNCONFIRMED")
    metadata = _metadata({"envs": response["created"]}, project)
    if metadata != expected:
        _fail("VERCEL_SETTINGS_CREATION_PARTIAL_OR_UNCONFIRMED")


def _unchanged(directory, hashes):
    for name, expected in hashes.items():
        if hashlib.sha256(_private_bytes(directory / name)).hexdigest() != expected:
            _fail("VERCEL_SETTINGS_PREPARED_FILES_CHANGED")


def configure_projects(directory, *, allow_cloud_settings=False, confirmation=None, cli=VERCEL_CLI):
    if allow_cloud_settings is not True:
        _fail("VERCEL_SETTINGS_EXPLICIT_APPROVAL_REQUIRED")
    if not sys.stdin.isatty():
        _fail("VERCEL_SETTINGS_INTERACTIVE_TERMINAL_REQUIRED")
    changes_possible = False
    verified = []
    try:
        directory, review, payloads, hashes = load_configuration(directory)
        cli = Path(cli).absolute()
        if not cli.is_file() or not os.access(cli, os.X_OK):
            _fail("VERCEL_SETTINGS_CLI_UNAVAILABLE")
        if confirmation is None:
            print("Add reviewed Production settings to both projects. Existing private Blob variables will be preserved.")
            print("Email sending and scanner access remain disabled. This command does not deploy either application.")
            confirmation = input("Type " + CONFIRMATION + " to continue: ")
        if confirmation != CONFIRMATION:
            _fail("VERCEL_SETTINGS_CONFIRMATION_MISMATCH")
        existing = {}
        for role, name in PROJECTS.items():
            project = review["projects"][role]
            metadata = _metadata(_request(cli, directory, "/v10/projects/" + name + "/env?decrypt=false"), project)
            _domains(_request(cli, directory, "/v9/projects/" + name + "/domains?production=true&redirects=false&limit=100"), project)
            if set(metadata) != set(BLOB_TYPES) or any(metadata[key]["type"] != kind for key, kind in BLOB_TYPES.items()):
                _fail("VERCEL_SETTINGS_EXISTING_VARIABLES_REQUIRE_REVIEW")
            if set(metadata).intersection(_expected_metadata(payloads[role])):
                _fail("VERCEL_SETTINGS_OVERWRITE_FORBIDDEN")
            existing[role] = metadata
        _unchanged(directory, hashes)
        for role, name in PROJECTS.items():
            project = review["projects"][role]
            expected = _expected_metadata(payloads[role])
            _unchanged(directory, hashes)
            changes_possible = True
            response = _request(
                cli, directory, "/v10/projects/" + project["id"] + "/env", method="POST",
                payload=directory / project["payload_file"], payload_sha256=project["sha256"],
            )
            _created(response, expected, project)
            metadata = _metadata(_request(cli, directory, "/v10/projects/" + project["id"] + "/env?decrypt=false"), project)
            if metadata != {**existing[role], **expected}:
                _fail("VERCEL_SETTINGS_READBACK_MISMATCH")
            verified.append(name)
        return {"scope": SCOPE, "status": "verified", "projects": [
            {"name": name, "settings_count": len(payloads[role]), "status": "verified"} for role, name in PROJECTS.items()
        ]}
    except ConfigurationError as error:
        error.changes_possible = changes_possible
        error.verified = tuple(verified)
        raise
    except (Exception, KeyboardInterrupt):
        raise ConfigurationError("VERCEL_SETTINGS_FAILED_REQUIRES_REVIEW", changes_possible=changes_possible, verified=verified) from None


def main(argv=None):
    parser = argparse.ArgumentParser(prog="configure-vercel-projects")
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--allow-cloud-settings", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        result = configure_projects(arguments.directory, allow_cloud_settings=arguments.allow_cloud_settings)
    except ConfigurationError as error:
        print(error.code, file=sys.stderr)
        if error.verified:
            print("Projects already verified: " + ", ".join(name for name in error.verified if name in PROJECTS.values()), file=sys.stderr)
        if error.changes_possible:
            print("Project settings may be partially changed. Keep the prepared files private. Do not rerun or delete settings; review the existing project metadata first.", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    print("Production settings verified. No deployment was performed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
