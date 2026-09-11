import hashlib
import os
import re
import secrets
import stat
from dataclasses import replace

from .backup import _mkdir_private, _new_file, _safe_path
from .database import Database, Session
from .errors import DomainError
from .migrations import load_migrations, validate_ledger
from .transfer import _validate_target


INSERT_TABLES = (
    "departments", "employees", "staff_accounts", "staff_account_roles", "staff_sessions",
    "locations", "scanner_devices", "meal_types", "qr_credentials", "email_queue",
    "audit_events", "serving_requests", "visitor_authorizations", "servings", "meals",
    "scan_attempts", "scan_app_requests", "employee_email_batches", "login_rate_limits",
    "employee_archives", "meal_voids", "master_qr_allocations",
)
UPDATE_TABLES = (
    "employees", "staff_accounts", "staff_sessions", "locations", "scanner_devices",
    "qr_credentials", "email_queue", "serving_requests", "visitor_authorizations",
    "scan_attempts", "login_rate_limits", "scan_app_requests", "employee_email_batches",
    "system_locks", "master_qr_allocations",
)
STAGES = (
    "PREFLIGHT", "PRIVATE_CREDENTIALS", "CREATE_ACCOUNT", "GRANTS",
    "VERIFY_ACCOUNT", "PUBLISH_CREDENTIALS",
)
ACCOUNT = ("meal_runtime", "%")


def target_confirmation(settings, runtime):
    target = settings.db_host + ":" + str(settings.db_port) + "/" + settings.db_name
    _validate_target(settings, runtime, target)
    if settings.db_name != "defaultdb" or settings.db_user.lower() == ACCOUNT[0]:
        raise DomainError("RUNTIME_ACCOUNT_MIGRATION_OWNER_AND_DEFAULTDB_REQUIRED")
    return "CREATE meal_runtime@% ON " + target


def _paths(output_path):
    output = _safe_path(output_path)
    pending = _safe_path(output.with_suffix(".pending.env"))
    if output.name != "database-runtime.env":
        raise DomainError("RUNTIME_ACCOUNT_INVALID_CREDENTIALS_FILENAME")
    if output.exists() or pending.exists():
        raise DomainError("RUNTIME_ACCOUNT_CREDENTIALS_ALREADY_EXIST_REVIEW_REQUIRED")
    _mkdir_private(output.parent)
    attributes = output.parent.stat()
    if attributes.st_uid != os.getuid() or stat.S_IMODE(attributes.st_mode) != 0o700:
        raise DomainError("RUNTIME_ACCOUNT_PRIVATE_DIRECTORY_REQUIRED")
    return output, pending


def _dotenv_value(value):
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _save_pending(path, settings):
    values = {
        "DB_HOST": settings.db_host, "DB_PORT": settings.db_port, "DB_NAME": settings.db_name,
        "DB_USER": settings.db_user, "DB_PASSWORD": settings.db_password,
        "DB_CONNECT_TIMEOUT": settings.db_connect_timeout, "DB_SSL_CA": settings.db_ssl_ca,
    }
    with _new_file(path) as stream:
        if stat.S_IMODE(os.fstat(stream.fileno()).st_mode) != 0o600:
            raise DomainError("RUNTIME_ACCOUNT_PRIVATE_FILE_REQUIRED")
        stream.write("".join(key + "=" + _dotenv_value(value) + "\n" for key, value in values.items()).encode("utf-8"))


def _sync_directory(path):
    descriptor = os.open(_safe_path(path), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _check_server(tx, settings, migrations, *, new_account=False):
    server = tx.one(
        "SELECT VERSION() AS version, DATABASE() AS name, CURRENT_USER() AS account, "
        "CURRENT_ROLE() AS active_role, @@GLOBAL.mandatory_roles AS mandatory_roles, "
        "@@SESSION.time_zone AS time_zone"
    )
    if (
        not server or not isinstance(server.get("version"), str)
        or not server["version"].startswith("8.4.") or server.get("name") != settings.db_name
        or server.get("time_zone") != "+00:00"
    ):
        raise DomainError("RUNTIME_ACCOUNT_MYSQL_8_4_UTC_TARGET_REQUIRED")
    if server.get("mandatory_roles") != "":
        raise DomainError("RUNTIME_ACCOUNT_MANDATORY_ROLES_NOT_ALLOWED")
    if new_account and (server.get("account") != "meal_runtime@%" or server.get("active_role") != "NONE"):
        raise DomainError("RUNTIME_ACCOUNT_IDENTITY_OR_ROLES_MISMATCH")
    tls = tx.one("SHOW SESSION STATUS LIKE 'Ssl_cipher'")
    if not tls or not tls.get("Value"):
        raise DomainError("RUNTIME_ACCOUNT_TLS_NOT_NEGOTIATED")
    ledger = tx.all("SELECT version, name, checksum, status FROM schema_migrations ORDER BY version")
    if validate_ledger(migrations, ledger):
        raise DomainError("RUNTIME_ACCOUNT_MIGRATIONS_NOT_CURRENT")


def _expected_grants():
    expected = {("*.*", "USAGE"), ("`defaultdb`.*", "SELECT")}
    for privilege, tables in (("INSERT", INSERT_TABLES), ("UPDATE", UPDATE_TABLES)):
        expected.update(("`defaultdb`.`" + table + "`", privilege) for table in tables)
    return expected


def _check_grants(rows):
    actual = set()
    pattern = (
        r"GRANT ([A-Z_, ]+) ON (\*\.\*|`defaultdb`\.\*|`defaultdb`\.`[a-z_]+`) "
        r"TO (?:`meal_runtime`@`%`|'meal_runtime'@'%')"
    )
    for row in rows:
        if not isinstance(row, dict) or len(row) != 1:
            raise DomainError("RUNTIME_ACCOUNT_GRANTS_MISMATCH")
        statement = next(iter(row.values()))
        match = re.fullmatch(pattern, statement) if isinstance(statement, str) else None
        if match is None:
            raise DomainError("RUNTIME_ACCOUNT_GRANTS_MISMATCH")
        privileges = match[1].split(", ")
        if any(privilege not in {"USAGE", "SELECT", "INSERT", "UPDATE"} for privilege in privileges):
            raise DomainError("RUNTIME_ACCOUNT_GRANTS_MISMATCH")
        actual.update((match[2], privilege) for privilege in privileges)
    if actual != _expected_grants():
        raise DomainError("RUNTIME_ACCOUNT_GRANTS_MISMATCH")


def _check_account(tx):
    definition = tx.one("SHOW CREATE USER CURRENT_USER()")
    if not isinstance(definition, dict) or len(definition) != 1:
        raise DomainError("RUNTIME_ACCOUNT_SSL_REQUIREMENT_NOT_VERIFIED")
    statement = next(iter(definition.values()))
    if not isinstance(statement, str):
        raise DomainError("RUNTIME_ACCOUNT_SSL_REQUIREMENT_NOT_VERIFIED")
    visible = re.sub(r"'(?:\\.|''|[^'\\])*'|`(?:``|[^`])*`", "__literal__", statement)
    if "'" in visible or "`" in visible or re.findall(r"\bREQUIRE\s+(SSL|NONE|X509|CIPHER|ISSUER|SUBJECT)\b", visible) != ["SSL"]:
        raise DomainError("RUNTIME_ACCOUNT_SSL_REQUIREMENT_NOT_VERIFIED")
    if re.findall(r"\bACCOUNT\s+(UNLOCK|LOCK)\b", visible) != ["UNLOCK"]:
        raise DomainError("RUNTIME_ACCOUNT_MUST_BE_UNLOCKED")
    _check_grants(tx.all("SHOW GRANTS"))


def _failure(error, stage, pending_exists):
    if isinstance(error, DomainError):
        failure = error
    else:
        failure = DomainError("RUNTIME_ACCOUNT_SETUP_FAILED")
    failure.runtime_account_stage = stage
    number = getattr(error, "errno", None)
    if type(number) is int and 1 <= number <= 65535:
        failure.runtime_account_mysql_error = number
    failure.runtime_account_pending_credentials = pending_exists
    return failure


def create_aiven_runtime_account(settings, runtime, migration_directory, output_path, *,
                                confirmation, allow_database_changes=False):
    if allow_database_changes is not True:
        raise DomainError("EXPLICIT_DATABASE_CHANGE_APPROVAL_REQUIRED")
    expected = target_confirmation(settings, runtime)
    if confirmation != expected:
        raise DomainError("RUNTIME_ACCOUNT_TARGET_NOT_CONFIRMED")
    migrations = load_migrations(migration_directory)
    if [migration.version for migration in migrations] != [1, 2, 3, 4, 5, 6, 7, 8, 9]:
        raise DomainError("RUNTIME_ACCOUNT_REQUIRES_REVIEWED_MIGRATIONS_ONE_TO_NINE")
    output, pending = _paths(output_path)
    connection = None
    runtime_connection = None
    tx = None
    locks = []
    stage = "PREFLIGHT"
    try:
        connection = Database(settings)._connect()
        tx = Session(connection)
        suffix = hashlib.sha256(settings.db_name.encode()).hexdigest()[:40]
        for lock in ("meal-runtime-account", "meal-migrations-" + suffix):
            result = tx.one("SELECT GET_LOCK(%s, 0) AS acquired", (lock,))
            if not result or result.get("acquired") != 1:
                raise DomainError("RUNTIME_ACCOUNT_DATABASE_OPERATION_ALREADY_RUNNING")
            locks.append(lock)
        _check_server(tx, settings, migrations)
        candidate = replace(settings, db_user=ACCOUNT[0], db_password=secrets.token_urlsafe(48) + "aA1!")
        stage = "PRIVATE_CREDENTIALS"
        _save_pending(pending, candidate)
        _sync_directory(pending.parent)
        stage = "CREATE_ACCOUNT"
        tx.execute("CREATE USER %s@%s IDENTIFIED BY %s REQUIRE SSL", (*ACCOUNT, candidate.db_password))
        stage = "GRANTS"
        tx.execute("GRANT SELECT ON `defaultdb`.* TO %s@%s", ACCOUNT)
        for privilege, tables in (("INSERT", INSERT_TABLES), ("UPDATE", UPDATE_TABLES)):
            for table in tables:
                tx.execute("GRANT " + privilege + " ON `defaultdb`.`" + table + "` TO %s@%s", ACCOUNT)
        connection.commit()
        stage = "VERIFY_ACCOUNT"
        runtime_connection = Database(candidate)._connect()
        runtime_tx = Session(runtime_connection)
        _check_server(runtime_tx, candidate, migrations, new_account=True)
        _check_account(runtime_tx)
        stage = "PUBLISH_CREDENTIALS"
        os.link(pending, output, follow_symlinks=False)
        _sync_directory(output.parent)
        pending.unlink()
        _sync_directory(output.parent)
        return {
            "status": "verified", "account": "meal_runtime@%", "credentials_file": str(output),
            "target": settings.db_host + ":" + str(settings.db_port) + "/" + settings.db_name,
        }
    except (Exception, KeyboardInterrupt) as error:
        raise _failure(error, stage, pending.exists()) from None
    finally:
        if runtime_connection is not None:
            try:
                runtime_connection.close()
            except Exception:
                pass
        if tx is not None:
            for lock in reversed(locks):
                try:
                    tx.one("SELECT RELEASE_LOCK(%s) AS released", (lock,))
                except Exception:
                    pass
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
