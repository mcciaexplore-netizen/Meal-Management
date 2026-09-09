import importlib.util
import os
import re
import unittest
import uuid
from pathlib import Path


MYSQL_TESTS_ENABLED = (
    os.environ.get("MEAL_RUN_MYSQL_TESTS") == "1"
    and os.environ.get("MEAL_ALLOW_TEST_SCHEMA_CHANGES") == "1"
    and bool(re.fullmatch(r"[A-Za-z0-9_]+_test", os.environ.get("MEAL_TEST_DB_NAME", "")))
)


def admin_connection_options(settings):
    options = {
        "host": settings.db_host,
        "port": settings.db_port,
        "user": settings.db_user,
        "password": settings.db_password,
        "connection_timeout": settings.db_connect_timeout,
        "autocommit": True,
    }
    if settings.db_ssl_ca:
        options.update(
            ssl_ca=settings.db_ssl_ca,
            ssl_verify_cert=True,
            ssl_verify_identity=True,
        )
    return options


def require_mysql_84(version):
    if not isinstance(version, str) or not re.match(r"^8\.4(?:\.|$)", version):
        raise AssertionError("Integration tests require MySQL 8.4")


def schema_statements(source):
    delimiter = ";"
    pending = []
    for line in source.splitlines():
        stripped = line.strip()
        if not pending and stripped.upper().startswith("DELIMITER "):
            delimiter = stripped.split(maxsplit=1)[1]
            continue
        if not stripped and not pending:
            continue
        pending.append(line)
        if stripped.endswith(delimiter):
            statement = "\n".join(pending).strip()
            yield statement[:-len(delimiter)].rstrip()
            pending = []
    if any(line.strip() for line in pending):
        raise ValueError("Schema contains an unterminated statement")


class IsolatedMySQLTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not MYSQL_TESTS_ENABLED:
            raise unittest.SkipTest("MySQL integration requires explicit connection and disposable-schema opt-in")
        for module in ("mysql", "cryptography", "qrcode"):
            if importlib.util.find_spec(module) is None:
                raise unittest.SkipTest(f"MySQL integration dependency is unavailable: {module}")
        import mysql.connector

        from cryptography.fernet import Fernet
        from meal_management.config import Settings
        from meal_management.database import Database

        environment = dict(os.environ)
        prefix = environment["MEAL_TEST_DB_NAME"]
        if len(prefix) > 35:
            raise ValueError("MEAL_TEST_DB_NAME must contain at most 35 characters")
        cls.database_name = f"{prefix}_{uuid.uuid4().hex[:16]}_test"
        environment["DB_NAME"] = cls.database_name
        environment["QR_ENCRYPTION_KEYS"] = Fernet.generate_key().decode()
        cls.settings = Settings.from_env(environment)
        cls.admin_connection = mysql.connector.connect(**admin_connection_options(cls.settings))
        cls.addClassCleanup(cls.admin_connection.close)
        cursor = cls.admin_connection.cursor()
        try:
            cursor.execute("SELECT VERSION()")
            require_mysql_84(cursor.fetchone()[0])
            cursor.execute(
                f"CREATE DATABASE `{cls.database_name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
            )
        finally:
            cursor.close()
        cls.addClassCleanup(cls.drop_database)
        cls.admin_connection.database = cls.database_name
        cursor = cls.admin_connection.cursor()
        try:
            source = (
                Path(__file__).resolve().parents[2] / "database" / "schema.sql"
            ).read_text(encoding="utf-8")
            for statement in schema_statements(source):
                normalized = statement.upper()
                if normalized.startswith("CREATE DATABASE ") or normalized.startswith("USE "):
                    continue
                cursor.execute(statement)
        finally:
            cursor.close()
        cls.db = Database(cls.settings)
        cls.test_environment = environment

    @classmethod
    def drop_database(cls):
        cursor = cls.admin_connection.cursor()
        try:
            cursor.execute(f"DROP DATABASE `{cls.database_name}`")
        finally:
            cursor.close()

    def rows(self, statement, parameters=()):
        cursor = self.admin_connection.cursor(dictionary=True)
        try:
            cursor.execute(statement, parameters)
            return cursor.fetchall()
        finally:
            cursor.close()

    def scalar(self, statement, parameters=()):
        rows = self.rows(statement, parameters)
        return next(iter(rows[0].values()))
