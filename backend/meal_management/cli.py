import argparse
import getpass
import json
import sys
import time
import warnings
from dataclasses import replace
from pathlib import Path

from .accounts import StaffService
from .config import Settings
from .database import Database
from .development_seed import DevelopmentSeedService, WAITERS, require_development_seed
from .delivery import delivery_from_settings
from .email_worker import EmailDeliveryWorker
from .errors import DomainError
from .migrations import MigrationRunner, load_migrations
from .runtime import RuntimeSettings
from .security import QrRenderer, TokenVault


def build_parser():
    parser = argparse.ArgumentParser(prog="meal-management")
    parser.add_argument("--env-file", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check-config")
    plan = commands.add_parser("migration-plan")
    plan.add_argument("--directory", type=Path, default=Path("database/migrations"))
    inspect = commands.add_parser("schema-inspect")
    inspect.add_argument("--directory", type=Path, default=Path("database/migrations"))
    inspect.add_argument("--allow-database-access", action="store_true")
    migrate = commands.add_parser("migrate")
    migrate.add_argument("--directory", type=Path, default=Path("database/migrations"))
    migrate.add_argument("--allow-database-changes", action="store_true")
    baseline = commands.add_parser("baseline")
    baseline.add_argument("--directory", type=Path, default=Path("database/migrations"))
    baseline.add_argument("--version", type=int, required=True)
    baseline.add_argument("--expected-schema-fingerprint", required=True)
    baseline.add_argument("--allow-database-changes", action="store_true")
    for command in (inspect, migrate, baseline):
        command.add_argument(
            "--database-user",
            help="Temporarily use this MySQL account and securely prompt for its password.",
        )
    bootstrap = commands.add_parser("bootstrap-admin")
    bootstrap.add_argument("--allow-database-access", action="store_true")
    seed = commands.add_parser("seed-development")
    seed.add_argument("--allow-database-changes", action="store_true")
    send = commands.add_parser("send-email", help="Send one explicitly selected queued QR email.")
    send.add_argument("--email-id", type=int, required=True)
    send.add_argument("--allow-database-changes", action="store_true")
    send.add_argument("--allow-real-email", action="store_true")
    return parser


def _load_environment(path):
    if path is None:
        return
    if not path.is_file():
        raise DomainError("ENVIRONMENT_FILE_NOT_FOUND")
    try:
        from dotenv import load_dotenv
    except ImportError:
        raise DomainError("PYTHON_DOTENV_NOT_INSTALLED") from None
    load_dotenv(path, override=False)


def _database_settings(args):
    settings = Settings.from_env()
    username = getattr(args, "database_user", None)
    if username is None:
        return settings
    username = username.strip()
    if not username or len(username) > 32 or any(ord(char) < 32 or ord(char) == 127 for char in username):
        raise DomainError("INVALID_DATABASE_USER")
    if not sys.stdin.isatty():
        raise DomainError("DATABASE_PASSWORD_REQUIRES_INTERACTIVE_TERMINAL")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = getpass.getpass("MySQL password for " + username + ": ")
    except getpass.GetPassWarning:
        raise DomainError("SECURE_PASSWORD_PROMPT_UNAVAILABLE") from None
    if not password:
        raise DomainError("DATABASE_PASSWORD_REQUIRED")
    return replace(settings, db_user=username, db_password=password)


def _safe_failure_message(error):
    fallback = "Operation failed. Check configuration and database availability."
    try:
        from mysql.connector import Error as MySQLError
    except ImportError:
        return fallback
    if not isinstance(error, MySQLError):
        return fallback
    number = getattr(error, "errno", None)
    if type(number) is not int or not 1 <= number <= 65535:
        return fallback
    explanations = {
        1044: "The selected account lacks permission to access the configured database.",
        1045: "MySQL authentication failed. Check the selected account and its password.",
        1049: "The configured database does not exist on this server.",
        2002: "Could not connect to MySQL. Check that the server is running and the connection settings are correct.",
        2003: "Could not connect to MySQL. Check that the server is running and the connection settings are correct.",
        2026: "The MySQL TLS connection failed. Check the configured certificates and server TLS settings.",
    }
    explanation = explanations.get(
        number,
        "The database operation failed. Inspect the migration state before retrying database changes.",
    )
    return "MySQL error " + str(number) + ". " + explanation


def _secure_password(prompt):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            return getpass.getpass(prompt)
    except getpass.GetPassWarning:
        raise DomainError("SECURE_PASSWORD_PROMPT_UNAVAILABLE") from None


def _seed_development():
    runtime = RuntimeSettings.from_env()
    require_development_seed(runtime)
    if not sys.stdin.isatty():
        raise DomainError("DEVELOPMENT_SEED_REQUIRES_INTERACTIVE_TERMINAL")
    settings = Settings.from_env()
    if any(ord(character) < 32 or ord(character) == 127 for character in settings.db_host):
        raise DomainError("INVALID_DATABASE_HOST")
    target = settings.db_host + ":" + str(settings.db_port) + "/" + settings.db_name
    print("Development database target: " + target)
    if input("Type the exact database target to confirm: ") != target:
        raise DomainError("DEVELOPMENT_SEED_TARGET_NOT_CONFIRMED")
    email = input("Existing administrator email: ").strip()
    password = _secure_password("Existing administrator password: ")
    database = Database(settings)
    staff = StaffService(database)
    context = staff.authenticate(email, password)
    logout_failed = False
    try:
        service = DevelopmentSeedService(database, runtime, TokenVault(settings.qr_encryption_keys), QrRenderer())
        existing = service.manifest(context)
        waiter_passwords = None
        if not existing["waiters"]:
            passwords = []
            for _, display_name, waiter_email in WAITERS:
                print("New development account: " + display_name + " <" + waiter_email + ">")
                waiter_password = _secure_password("New waiter password, at least 12 characters: ")
                confirmation = _secure_password("Repeat waiter password: ")
                if waiter_password != confirmation:
                    raise DomainError("PASSWORD_CONFIRMATION_MISMATCH")
                if not 12 <= len(waiter_password) <= 1024:
                    raise DomainError("PASSWORD_LENGTH_INVALID")
                passwords.append(waiter_password)
            waiter_passwords = tuple(passwords)
        result = service.seed(context, waiter_passwords=waiter_passwords)
    finally:
        try:
            staff.logout(context)
        except Exception:
            logout_failed = True
    if result.created and result.expiry_wait_seconds:
        print("Waiting for the expired test credential to become invalid.")
        time.sleep(min(result.expiry_wait_seconds, 10))
    print(
        "Development test data created. QR emails remain in the local preview queue."
        if result.created else "Development test data already exists; fixture records and passwords were preserved."
    )
    if logout_failed:
        print("Temporary setup session could not be closed; it will expire automatically.", file=sys.stderr)
    return 0


def _send_email(args):
    if not 1 <= args.email_id <= 18446744073709551615:
        raise DomainError("INVALID_EMAIL_ID")
    if not args.allow_real_email:
        raise DomainError("EXPLICIT_REAL_EMAIL_APPROVAL_REQUIRED")
    runtime = RuntimeSettings.from_env()
    if runtime.email_backend not in {"gmail", "ses"}:
        raise DomainError("REAL_EMAIL_BACKEND_REQUIRED")
    if not runtime.email_send_enabled:
        raise DomainError("REAL_EMAIL_SENDING_DISABLED")
    settings = Settings.from_env()
    if runtime.environment == "production" and not settings.db_ssl_ca:
        raise DomainError("MISSING_SETTING_DB_SSL_CA")
    delivery = delivery_from_settings(runtime)
    worker = EmailDeliveryWorker(Database(settings), TokenVault(settings.qr_encryption_keys), QrRenderer(), delivery)
    result = worker.send(args.email_id, allow_real_email=True)
    print(json.dumps(result))
    return 0 if result["status"] in {"SENT", "ALREADY_SENT"} else 2


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        _load_environment(args.env_file)
        if args.command == "check-config":
            database_settings = Settings.from_env()
            runtime = RuntimeSettings.from_env()
            if runtime.environment == "production" and not database_settings.db_ssl_ca:
                raise DomainError("MISSING_SETTING_DB_SSL_CA")
            print("Configuration is valid. No database connection was made.")
            return 0
        if args.command == "migration-plan":
            migrations = load_migrations(args.directory)
            print(json.dumps([
                {"version": item.version, "name": item.name, "checksum": item.checksum}
                for item in migrations
            ], indent=2))
            return 0
        if args.command in {"migrate", "baseline", "seed-development", "send-email"} and not args.allow_database_changes:
            raise DomainError("EXPLICIT_DATABASE_CHANGE_APPROVAL_REQUIRED")
        if args.command in {"schema-inspect", "bootstrap-admin"} and not args.allow_database_access:
            raise DomainError("EXPLICIT_DATABASE_ACCESS_APPROVAL_REQUIRED")
        if args.command == "seed-development":
            return _seed_development()
        if args.command == "send-email":
            return _send_email(args)
        database = Database(_database_settings(args))
        if args.command == "bootstrap-admin":
            if not sys.stdin.isatty():
                raise DomainError("BOOTSTRAP_REQUIRES_INTERACTIVE_TERMINAL")
            display_name = input("Administrator name: ").strip()
            email = input("Administrator email: ").strip()
            password = getpass.getpass("New password, at least 12 characters: ")
            confirmation = getpass.getpass("Repeat password: ")
            if password != confirmation:
                raise DomainError("PASSWORD_CONFIRMATION_MISMATCH")
            StaffService(database).bootstrap_admin(display_name, email, password)
            print("First administrator created.")
            return 0
        runner = MigrationRunner(database, args.directory)
        if args.command == "schema-inspect":
            print(json.dumps(runner.inspect(), indent=2))
        elif args.command == "baseline":
            version = runner.baseline(args.version, args.expected_schema_fingerprint)
            print("Reviewed schema recorded at migration " + str(version) + ".")
        else:
            applied = runner.apply()
            print("Applied migrations: " + (", ".join(map(str, applied)) if applied else "none"))
        return 0
    except DomainError as error:
        missing = getattr(error, "missing", ())
        print(error.code + (": " + ", ".join(missing) if missing else ""), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Operation cancelled.", file=sys.stderr)
        return 130
    except Exception as error:
        print(_safe_failure_message(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
