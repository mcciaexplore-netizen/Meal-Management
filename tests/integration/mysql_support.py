import importlib.util
import os
import re
import secrets
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


POSTGRES_TESTS = os.environ.get("MEAL_TEST_ENGINE") == "postgres"
RUNTIME_PRIVILEGE_TESTS = os.environ.get("MEAL_TEST_RUNTIME_PRIVILEGES") == "1"


class IsolatedMySQLTestCase(unittest.TestCase):
    runtime_privileges = False

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if POSTGRES_TESTS:
            cls.setUpPostgres()
            return
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
        cls.admin_settings = cls.settings
        cls.restricted = RUNTIME_PRIVILEGE_TESTS or cls.runtime_privileges
        if cls.restricted:
            cls.settings = cls.runtime_privilege_settings()
            environment["DB_USER"] = cls.settings.db_user
            environment["DB_PASSWORD"] = cls.settings.db_password
        cls.db = Database(cls.settings)
        cls.test_environment = environment

    @classmethod
    def runtime_privilege_settings(cls):
        from dataclasses import replace

        from meal_management import runtime_account

        user = "mrt_" + uuid.uuid4().hex[:12]
        password = secrets.token_urlsafe(24) + "aA1!"
        database = "`" + cls.database_name + "`"
        cursor = cls.admin_connection.cursor()
        try:
            cursor.execute("CREATE USER %s@%s IDENTIFIED BY %s", (user, "%", password))
            cls.addClassCleanup(cls.drop_runtime_user, user)
            cursor.execute(
                "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s", (cls.database_name,)
            )
            existing = {table for (table,) in cursor.fetchall()}
            for table in sorted(existing):
                cursor.execute("GRANT SELECT ON " + database + ".`" + table + "` TO %s@%s", (user, "%"))
            for privilege, tables in (
                ("INSERT", runtime_account.INSERT_TABLES),
                ("UPDATE", runtime_account.UPDATE_TABLES),
                ("DELETE", runtime_account.DELETE_TABLES),
            ):
                for table in sorted(set(tables) & existing):
                    cursor.execute("GRANT " + privilege + " ON " + database + ".`" + table + "` TO %s@%s", (user, "%"))
        finally:
            cursor.close()
        return replace(cls.settings, db_user=user, db_password=password)

    @classmethod
    def drop_runtime_user(cls, user):
        cursor = cls.admin_connection.cursor()
        try:
            cursor.execute("DROP USER IF EXISTS %s@%s", (user, "%"))
        finally:
            cursor.close()

    @classmethod
    def setUpPostgres(cls):
        if not MYSQL_TESTS_ENABLED:
            raise unittest.SkipTest("PostgreSQL integration requires explicit connection and disposable-schema opt-in")
        import psycopg
        from psycopg.rows import dict_row

        from cryptography.fernet import Fernet
        from meal_management.config import Settings
        from meal_management.database import Database
        from meal_management.migrations import sql_statements

        environment = dict(os.environ)
        environment["DB_ENGINE"] = "postgres"
        cls.database_name = f"{environment['MEAL_TEST_DB_NAME']}_{uuid.uuid4().hex[:16]}_test"
        environment["DB_NAME"] = cls.database_name
        environment["QR_ENCRYPTION_KEYS"] = Fernet.generate_key().decode()
        cls.settings = Settings.from_env(environment)
        options = dict(host=cls.settings.db_host, port=cls.settings.db_port, user=cls.settings.db_user, password=cls.settings.db_password)
        with psycopg.connect(dbname="postgres", autocommit=True, **options) as server:
            server.execute(f"CREATE DATABASE {cls.database_name}")
        cls.admin_connection = psycopg.connect(dbname=cls.database_name, autocommit=True, row_factory=dict_row, **options)
        cls.addClassCleanup(cls.drop_postgres_database, options)
        source = (Path(__file__).resolve().parents[2] / "database" / "postgres" / "001_initial.sql").read_text(encoding="utf-8")
        for statement in sql_statements(source):
            cls.admin_connection.execute(statement)
        cls.admin_connection.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name VARCHAR(100) NOT NULL, "
            "checksum CHAR(64) NOT NULL, status VARCHAR(16) NOT NULL, started_at TIMESTAMP(6), applied_at TIMESTAMP(6))"
        )
        cls.admin_connection.execute("INSERT INTO schema_migrations VALUES (1, 'initial', repeat('0', 64), 'APPLIED', NULL, NULL)")
        cls.db = Database(cls.settings)
        cls.test_environment = environment

    @classmethod
    def drop_postgres_database(cls, options):
        import psycopg

        cls.admin_connection.close()
        with psycopg.connect(dbname="postgres", autocommit=True, **options) as server:
            server.execute(f"DROP DATABASE {cls.database_name} WITH (FORCE)")

    @classmethod
    def drop_database(cls):
        cursor = cls.admin_connection.cursor()
        try:
            cursor.execute(f"DROP DATABASE `{cls.database_name}`")
        finally:
            cursor.close()

    def rows(self, statement, parameters=()):
        if POSTGRES_TESTS:
            return self.admin_connection.execute(statement, parameters).fetchall()
        cursor = self.admin_connection.cursor(dictionary=True)
        try:
            cursor.execute(statement, parameters)
            return cursor.fetchall()
        finally:
            cursor.close()

    def scalar(self, statement, parameters=()):
        rows = self.rows(statement, parameters)
        return next(iter(rows[0].values()))
