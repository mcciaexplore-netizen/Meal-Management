import hashlib
import json
from datetime import datetime
from decimal import Decimal

from .database import Database, POSTGRES_IDENTITY_TABLES, Session
from .errors import DomainError


TABLES = (
    "departments",
    "employees",
    "staff_accounts",
    "roles",
    "staff_account_roles",
    "employee_archives",
    "system_locks",
    "staff_sessions",
    "login_rate_limits",
    "locations",
    "scanner_devices",
    "meal_types",
    "scan_app_settings",
    "scan_app_requests",
    "qr_credentials",
    "master_qr_allocations",
    "employee_email_batches",
    "email_queue",
    "audit_events",
    "serving_requests",
    "visitor_authorizations",
    "servings",
    "meals",
    "meal_voids",
    "scan_attempts",
    "development_seed_batches",
    "development_seed_records",
)

REFERENCE_ROWS = {
    "roles": 3,
    "system_locks": 1,
    "staff_accounts": 1,
    "staff_account_roles": 1,
    "locations": 1,
    "scanner_devices": 1,
    "meal_types": 1,
    "scan_app_settings": 1,
}

JSON_COLUMNS = {("audit_events", "before_data"), ("audit_events", "after_data")}
BATCH_SIZE = 500


def _canonical(table, column, value):
    if value is None:
        return None
    if (table, column) in JSON_COLUMNS:
        if isinstance(value, (bytes, bytearray)):
            value = value.decode("utf-8")
        if isinstance(value, str):
            value = json.loads(value)
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "b:" + bytes(value).hex()
    if isinstance(value, bool):
        return "i:" + str(int(value))
    if isinstance(value, int):
        return "i:" + str(value)
    if isinstance(value, Decimal):
        return "i:" + str(int(value)) if value == value.to_integral_value() else "d:" + str(value)
    if isinstance(value, datetime):
        return "t:" + value.isoformat(timespec="microseconds")
    return "s:" + str(value)


def _digest(table, columns, rows):
    lines = sorted(
        json.dumps([_canonical(table, column, row[column]) for column in columns], separators=(",", ":"))
        for row in rows
    )
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _source_value(table, column, value):
    if isinstance(value, bytearray):
        return bytes(value)
    if (table, column) in JSON_COLUMNS and isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8")
    return value


def _target_columns(tx, table):
    rows = tx.all(
        "SELECT a.attname AS name FROM pg_attribute a "
        "WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped AND a.attgenerated = '' "
        "ORDER BY a.attnum",
        (table,),
    )
    return [row["name"] for row in rows]


def _check_source(source_tx):
    version = source_tx.one("SELECT VERSION() AS version")["version"]
    if not version.startswith("8.4."):
        raise DomainError("TRANSFER_SOURCE_REQUIRES_MYSQL_8_4")
    applied = {row["version"] for row in source_tx.all("SELECT version FROM schema_migrations WHERE status = 'APPLIED'")}
    if not set(range(1, 10)).issubset(applied):
        raise DomainError("TRANSFER_SOURCE_MIGRATIONS_INCOMPLETE")


def _check_target(target_tx):
    applied = {row["version"] for row in target_tx.all("SELECT version FROM schema_migrations WHERE status = 'APPLIED'")}
    if 1 not in applied:
        raise DomainError("TRANSFER_TARGET_REQUIRES_POSTGRES_MIGRATION")
    for table in TABLES:
        count = target_tx.one("SELECT COUNT(*) AS total FROM " + table)["total"]
        if count != REFERENCE_ROWS.get(table, 0):
            raise DomainError("TRANSFER_TARGET_NOT_EMPTY")


def transfer_mysql_to_postgres(source_settings, target_settings, *, progress=print):
    if source_settings.db_engine != "mysql" or target_settings.db_engine != "postgres":
        raise DomainError("TRANSFER_REQUIRES_MYSQL_SOURCE_AND_POSTGRES_TARGET")
    source_connection = Database(source_settings)._connect()
    target_connection = Database(target_settings)._connect()
    try:
        source_connection.start_transaction(consistent_snapshot=True, isolation_level="REPEATABLE READ", readonly=True)
        source = Session(source_connection, "mysql")
        target = Session(target_connection, "postgres")
        _check_source(source)
        _check_target(target)
        target.execute("TRUNCATE " + ", ".join(TABLES) + " RESTART IDENTITY")
        summary = []
        for table in TABLES:
            columns = _target_columns(target, table)
            selected = ", ".join("`" + column + "`" for column in columns)
            rows = source.all("SELECT " + selected + " FROM `" + table + "`")
            statement = (
                "INSERT INTO " + table + " (" + ", ".join(columns) + ") VALUES ("
                + ", ".join(["%s"] * len(columns)) + ")"
            )
            values = [tuple(_source_value(table, column, row[column]) for column in columns) for row in rows]
            with target_connection.cursor() as cursor:
                for start in range(0, len(values), BATCH_SIZE):
                    cursor.executemany(statement, values[start:start + BATCH_SIZE])
            if table in POSTGRES_IDENTITY_TABLES:
                target.one(
                    "SELECT setval(pg_get_serial_sequence(%s, 'id'), COALESCE(MAX(id), 1), MAX(id) IS NOT NULL) AS value "
                    "FROM " + table,
                    (table,),
                )
            copied = target.all("SELECT " + ", ".join(columns) + " FROM " + table)
            if len(copied) != len(rows) or _digest(table, columns, copied) != _digest(table, columns, rows):
                raise DomainError("TRANSFER_VERIFICATION_FAILED")
            summary.append((table, len(rows)))
            progress(table + ": " + str(len(rows)) + " rows copied and verified")
        target_connection.commit()
        source_connection.rollback()
        return summary
    except BaseException:
        try:
            target_connection.rollback()
        except Exception:
            pass
        raise
    finally:
        for connection in (source_connection, target_connection):
            try:
                connection.close()
            except Exception:
                pass
