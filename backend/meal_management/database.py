from contextlib import contextmanager
from functools import wraps
import re
import time

from .errors import DependencyError, DomainError


def retry_transaction(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        for attempt in range(3):
            try:
                return function(*args, **kwargs)
            except Exception as error:
                if getattr(error, "errno", None) not in {1205, 1213}:
                    raise
                if attempt == 2:
                    raise DomainError("TRANSACTION_RETRY_REQUIRED") from None
                time.sleep(0.02 * (attempt + 1))
    return wrapped


POSTGRES_ERROR_NUMBERS = {
    "23505": 1062,
    "23503": 1452,
    "23514": 3819,
    "40P01": 1213,
    "40001": 1213,
    "55P03": 1205,
    "42703": 1054,
    "42P01": 1146,
    "45000": 1644,
    "28P01": 1045,
    "3D000": 1049,
    "42501": 1044,
}

POSTGRES_IDENTITY_TABLES = frozenset({
    "audit_events",
    "departments",
    "email_queue",
    "employee_email_batches",
    "employees",
    "locations",
    "meal_types",
    "meals",
    "qr_credentials",
    "scan_attempts",
    "scanner_devices",
    "servings",
    "staff_accounts",
    "staff_sessions",
    "visitor_authorizations",
})


class DatabaseError(Exception):
    def __init__(self, errno, sqlstate):
        super().__init__("DATABASE_ERROR")
        self.errno = errno
        self.sqlstate = sqlstate


@contextmanager
def _postgres_errors():
    import psycopg

    try:
        yield
    except psycopg.Error as error:
        sqlstate = getattr(error, "sqlstate", None)
        raise DatabaseError(POSTGRES_ERROR_NUMBERS.get(sqlstate), sqlstate) from None


class Session:
    def __init__(self, connection, dialect="mysql"):
        self.connection = connection
        self.dialect = dialect

    def _postgres_cursor(self, rows):
        from psycopg.rows import dict_row

        return self.connection.cursor(row_factory=dict_row) if rows else self.connection.cursor()

    def one(self, sql, params=()):
        if self.dialect == "postgres":
            with _postgres_errors(), self._postgres_cursor(True) as cursor:
                cursor.execute(sql, tuple(params))
                return cursor.fetchone()
        cursor = self.connection.cursor(dictionary=True, buffered=True)
        try:
            cursor.execute(sql, tuple(params))
            return cursor.fetchone()
        finally:
            cursor.close()

    def all(self, sql, params=()):
        if self.dialect == "postgres":
            with _postgres_errors(), self._postgres_cursor(True) as cursor:
                cursor.execute(sql, tuple(params))
                return cursor.fetchall()
        cursor = self.connection.cursor(dictionary=True, buffered=True)
        try:
            cursor.execute(sql, tuple(params))
            return cursor.fetchall()
        finally:
            cursor.close()

    def execute(self, sql, params=()):
        if self.dialect == "postgres":
            with _postgres_errors(), self._postgres_cursor(False) as cursor:
                cursor.execute(sql, tuple(params))
                return cursor.rowcount
        cursor = self.connection.cursor(buffered=True)
        try:
            cursor.execute(sql, tuple(params))
            return cursor.rowcount
        finally:
            cursor.close()

    def insert(self, sql, params=()):
        if self.dialect == "postgres":
            match = re.match(r"\s*INSERT\s+INTO\s+([a-z_]+)", sql, re.IGNORECASE)
            if match is None or match[1].lower() not in POSTGRES_IDENTITY_TABLES:
                self.execute(sql, params)
                return None
            with _postgres_errors(), self._postgres_cursor(False) as cursor:
                cursor.execute(sql + " RETURNING id", tuple(params))
                return cursor.fetchone()[0]
        cursor = self.connection.cursor(buffered=True)
        try:
            cursor.execute(sql, tuple(params))
            return cursor.lastrowid
        finally:
            cursor.close()

    def now(self):
        if self.dialect == "postgres":
            return self.one("SELECT (statement_timestamp() AT TIME ZONE 'UTC')::timestamp(6) AS now")["now"]
        return self.one("SELECT UTC_TIMESTAMP(6) AS now")["now"]


def is_postgres(tx):
    return getattr(tx, "dialect", "mysql") == "postgres"


def ignore_duplicate(tx, column):
    if is_postgres(tx):
        return " ON CONFLICT DO NOTHING"
    return " ON DUPLICATE KEY UPDATE " + column + " = " + column


def null_safe_equal(tx, column):
    if is_postgres(tx):
        return column + " IS NOT DISTINCT FROM %s"
    return column + " <=> %s"


def typed_null(tx, sql_type):
    if is_postgres(tx):
        return "CAST(NULL AS " + sql_type + ")"
    return "NULL"


@contextmanager
def savepoint(tx):
    if not is_postgres(tx):
        yield
        return
    with tx.connection.transaction():
        yield


def _postgres_connection(settings):
    try:
        import psycopg
        from psycopg.adapt import Dumper
    except ImportError:
        raise DependencyError("POSTGRES_DRIVER_NOT_INSTALLED") from None

    class BooleanAsInteger(Dumper):
        oid = psycopg.adapters.types["int2"].oid

        def dump(self, obj):
            return b"1" if obj else b"0"

    options = {
        "host": settings.db_host,
        "port": settings.db_port,
        "dbname": settings.db_name,
        "user": settings.db_user,
        "password": settings.db_password,
        "connect_timeout": settings.db_connect_timeout,
        "autocommit": False,
        "prepare_threshold": None,
        "application_name": "meal-management",
        "options": "-c TimeZone=UTC -c lock_timeout=50s -c idle_in_transaction_session_timeout=120s",
    }
    if settings.db_ssl_ca:
        options.update(sslmode="verify-full", sslrootcert=settings.db_ssl_ca)
    else:
        options.update(sslmode="prefer")
    with _postgres_errors():
        connection = psycopg.connect(**options)
    connection.adapters.register_dumper(bool, BooleanAsInteger)
    connection.isolation_level = psycopg.IsolationLevel.READ_COMMITTED
    return connection


class Database:
    def __init__(self, settings=None, connection_factory=None, dialect=None):
        if settings is None and connection_factory is None:
            raise ValueError("Database configuration is required")
        self.settings = settings
        self.connection_factory = connection_factory
        self.dialect = dialect or getattr(settings, "db_engine", None) or "mysql"

    def _connect(self):
        if self.connection_factory is not None:
            return self.connection_factory()
        if self.dialect == "postgres":
            return _postgres_connection(self.settings)
        try:
            import mysql.connector
        except ImportError:
            raise DependencyError("MYSQL_CONNECTOR_NOT_INSTALLED") from None
        options = {
            "host": self.settings.db_host,
            "port": self.settings.db_port,
            "database": self.settings.db_name,
            "user": self.settings.db_user,
            "password": self.settings.db_password,
            "connection_timeout": self.settings.db_connect_timeout,
            "charset": "utf8mb4",
            "collation": "utf8mb4_0900_ai_ci",
            "autocommit": False,
            "time_zone": "+00:00",
            "sql_mode": "ONLY_FULL_GROUP_BY,STRICT_TRANS_TABLES,NO_ZERO_IN_DATE,NO_ZERO_DATE,ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION",
        }
        if self.settings.db_ssl_ca:
            options.update(ssl_ca=self.settings.db_ssl_ca, ssl_verify_cert=True, ssl_verify_identity=True)
        return mysql.connector.connect(**options)

    @contextmanager
    def transaction(self):
        connection = self._connect()
        try:
            if self.dialect != "postgres":
                connection.start_transaction(isolation_level="READ COMMITTED")
            yield Session(connection, self.dialect)
            if self.dialect == "postgres":
                with _postgres_errors():
                    connection.commit()
            else:
                connection.commit()
        except BaseException:
            try:
                connection.rollback()
            except Exception:
                pass
            raise
        finally:
            try:
                connection.close()
            except Exception:
                pass
