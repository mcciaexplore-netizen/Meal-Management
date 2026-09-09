from contextlib import contextmanager
from functools import wraps
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


class Session:
    def __init__(self, connection):
        self.connection = connection

    def one(self, sql, params=()):
        cursor = self.connection.cursor(dictionary=True, buffered=True)
        try:
            cursor.execute(sql, tuple(params))
            return cursor.fetchone()
        finally:
            cursor.close()

    def all(self, sql, params=()):
        cursor = self.connection.cursor(dictionary=True, buffered=True)
        try:
            cursor.execute(sql, tuple(params))
            return cursor.fetchall()
        finally:
            cursor.close()

    def execute(self, sql, params=()):
        cursor = self.connection.cursor(buffered=True)
        try:
            cursor.execute(sql, tuple(params))
            return cursor.rowcount
        finally:
            cursor.close()

    def insert(self, sql, params=()):
        cursor = self.connection.cursor(buffered=True)
        try:
            cursor.execute(sql, tuple(params))
            return cursor.lastrowid
        finally:
            cursor.close()

    def now(self):
        return self.one("SELECT UTC_TIMESTAMP(6) AS now")["now"]


class Database:
    def __init__(self, settings=None, connection_factory=None):
        if settings is None and connection_factory is None:
            raise ValueError("Database configuration is required")
        self.settings = settings
        self.connection_factory = connection_factory

    def _connect(self):
        if self.connection_factory is not None:
            return self.connection_factory()
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
            connection.start_transaction(isolation_level="READ COMMITTED")
            yield Session(connection)
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
