"""Create and verify the restricted meal_runtime account on Amazon RDS for MySQL.

Run inside the application image as a one-off container. The master account
comes from the container environment (an env file with DB_USER/DB_PASSWORD for
the RDS master user); the meal_runtime password comes from the application's
.env.docker, mounted read-only:

    docker run --rm --user "$(id -u):$(id -g)" \\
        --env-file ~/mccia-rds/rds-admin.env \\
        -v "$PWD/var/private/db-ca.pem:/run/db-ca.pem:ro" \\
        -v "$PWD/.env.docker:/run/app.env:ro" \\
        -v "$PWD/deploy/docker/rds-runtime-account.py:/run/rds-runtime-account.py:ro" \\
        meal-management:latest python /run/rds-runtime-account.py --runtime-env /run/app.env

The grants are the same as create-aiven-runtime (meal_management.runtime_account):
SELECT on the database plus table-specific INSERT and UPDATE, with no DELETE,
schema, account-management or GRANT OPTION privileges. The account requires
TLS and is limited to the VPC address range given by --host-scope.
Pass --verify-only to re-check an existing account without changing anything.
No password is ever printed.
"""

import argparse
import re
import sys
from pathlib import Path

import mysql.connector

from meal_management.config import Settings
from meal_management.migrations import load_migrations
from meal_management.runtime_account import INSERT_TABLES, UPDATE_TABLES

ACCOUNT = "meal_runtime"
PROJECT_ROOT = Path("/app")


def read_env(path):
    values = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def connect(settings, user, password):
    return mysql.connector.connect(
        host=settings.db_host, port=settings.db_port, database=settings.db_name,
        user=user, password=password, connection_timeout=settings.db_connect_timeout,
        ssl_ca=settings.db_ssl_ca, ssl_verify_cert=True, ssl_verify_identity=True, autocommit=True,
    )


def text(value):
    return value.decode() if isinstance(value, (bytes, bytearray)) else str(value)


def account_rows(cursor):
    cursor.execute("SELECT host, ssl_type FROM mysql.user WHERE user = %s", (ACCOUNT,))
    return [(text(host), text(ssl_type)) for host, ssl_type in cursor.fetchall()]


def expected_grants(database):
    expected = {("*.*", "USAGE"), ("`" + database + "`.*", "SELECT")}
    for privilege, tables in (("INSERT", INSERT_TABLES), ("UPDATE", UPDATE_TABLES)):
        expected.update(("`" + database + "`.`" + table + "`", privilege) for table in tables)
    return expected


def actual_grants(rows, host_scope):
    grantee = re.escape("`" + ACCOUNT + "`@`" + host_scope + "`")
    pattern = re.compile(r"GRANT ([A-Z_, ]+) ON (\S+) TO " + grantee + r"$")
    actual = set()
    for (row,) in rows:
        match = pattern.fullmatch(text(row))
        if not match:
            raise SystemExit("Unexpected grant statement for " + ACCOUNT + "; review SHOW GRANTS manually.")
        actual.update((match[2], privilege.strip()) for privilege in match[1].split(","))
    return actual


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-env", required=True, help="The application's .env.docker (mounted read-only).")
    parser.add_argument("--host-scope", default="172.31.%", help="MySQL host part for the account (the VPC range).")
    parser.add_argument("--verify-only", action="store_true")
    arguments = parser.parse_args()
    if not re.fullmatch(r"[0-9.%]+", arguments.host_scope) or arguments.host_scope == "%":
        raise SystemExit("--host-scope must be a VPC address pattern such as 172.31.%.")

    master = Settings.from_env()
    if not master.db_ssl_ca:
        raise SystemExit("DB_SSL_CA is required.")
    runtime = read_env(arguments.runtime_env)
    if runtime.get("DB_USER") != ACCOUNT or not runtime.get("DB_PASSWORD"):
        raise SystemExit("The runtime env file must set DB_USER=meal_runtime and DB_PASSWORD.")
    if (runtime.get("DB_HOST", "").lower(), runtime.get("DB_NAME")) != (master.db_host.lower(), master.db_name):
        raise SystemExit("The runtime env file targets a different database host or name.")
    if master.db_user == ACCOUNT:
        raise SystemExit("Run this with the RDS master account, not meal_runtime.")

    connection = connect(master, master.db_user, master.db_password)
    cursor = connection.cursor()
    cursor.execute("SELECT VERSION(), DATABASE()")
    version, database = (text(value) for value in cursor.fetchone())
    if not version.startswith("8.4.") or database != master.db_name:
        raise SystemExit("Expected MySQL 8.4 and database " + master.db_name + ".")
    cursor.execute("SELECT version FROM schema_migrations WHERE status = 'APPLIED' ORDER BY version")
    applied = [row[0] for row in cursor.fetchall()]
    required = [migration.version for migration in load_migrations(PROJECT_ROOT / "database" / "migrations")]
    if applied != required:
        raise SystemExit("Apply all migrations before creating the runtime account.")
    accounts = account_rows(cursor)

    if not arguments.verify_only:
        if accounts:
            raise SystemExit(ACCOUNT + " already exists; rerun with --verify-only to check it.")
        cursor.execute(
            "CREATE USER %s@%s IDENTIFIED BY %s REQUIRE SSL",
            (ACCOUNT, arguments.host_scope, runtime["DB_PASSWORD"]),
        )
        cursor.execute("GRANT SELECT ON `" + database + "`.* TO %s@%s", (ACCOUNT, arguments.host_scope))
        for privilege, tables in (("INSERT", INSERT_TABLES), ("UPDATE", UPDATE_TABLES)):
            for table in tables:
                cursor.execute(
                    "GRANT " + privilege + " ON `" + database + "`.`" + table + "` TO %s@%s",
                    (ACCOUNT, arguments.host_scope),
                )
        accounts = account_rows(cursor)
    connection.close()

    if accounts != [(arguments.host_scope, "ANY")]:
        raise SystemExit("Expected exactly one " + ACCOUNT + "@" + arguments.host_scope + " account with REQUIRE SSL.")

    connection = connect(master, ACCOUNT, runtime["DB_PASSWORD"])
    cursor = connection.cursor()
    cursor.execute("SELECT CURRENT_USER()")
    current = text(cursor.fetchone()[0])
    cursor.execute("SHOW SESSION STATUS LIKE 'Ssl_cipher'")
    cipher = text(cursor.fetchone()[1])
    cursor.execute("SHOW GRANTS")
    grants = actual_grants(cursor.fetchall(), arguments.host_scope)
    cursor.execute("SELECT COUNT(*) FROM schema_migrations")
    cursor.fetchone()
    connection.close()

    if current != ACCOUNT + "@" + arguments.host_scope:
        raise SystemExit("Logged in as an unexpected account.")
    if not cipher:
        raise SystemExit("The runtime connection did not negotiate TLS.")
    if grants != expected_grants(database):
        missing = sorted(expected_grants(database) - grants)
        extra = sorted(grants - expected_grants(database))
        raise SystemExit("Grant mismatch. Missing: " + str(missing) + " Extra: " + str(extra))
    print(
        ("Verified" if arguments.verify_only else "Created and verified") + " " + current
        + ": REQUIRE SSL, TLS " + cipher + ", SELECT on " + database + ".*, INSERT on "
        + str(len(INSERT_TABLES)) + " tables, UPDATE on " + str(len(UPDATE_TABLES))
        + " tables, nothing else."
    )


if __name__ == "__main__":
    sys.exit(main())
