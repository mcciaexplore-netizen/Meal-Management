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


PROJECT_ROOT = Path(__file__).resolve().parents[2]


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
    backup = commands.add_parser("backup-local")
    backup.add_argument("--allow-database-access", action="store_true")
    backup.add_argument("--writers-paused", action="store_true")
    backup.add_argument("--directory", type=Path, default=PROJECT_ROOT / "database" / "migrations")
    restore = commands.add_parser("restore-aiven")
    restore.add_argument("--backup", type=Path, required=True)
    restore.add_argument("--expected-target", required=True)
    restore.add_argument("--allow-database-changes", action="store_true")
    restore.add_argument("--writers-paused", action="store_true")
    restore.add_argument("--directory", type=Path, default=PROJECT_ROOT / "database" / "migrations")
    account = commands.add_parser("create-aiven-runtime", help="Review and create the restricted Aiven account for Vercel.")
    account.add_argument("--allow-database-changes", action="store_true", help="Allow account creation only after interactive target confirmation.")
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


def _transfer_environment(path):
    if path is None:
        raise DomainError("EXPLICIT_ENV_FILE_REQUIRED")
    path = path.expanduser().absolute()
    if not path.is_file():
        raise DomainError("ENVIRONMENT_FILE_NOT_FOUND")
    try:
        from dotenv import dotenv_values
    except ImportError:
        raise DomainError("PYTHON_DOTENV_NOT_INSTALLED") from None
    values = dict(dotenv_values(path, interpolate=False))
    if (values.get("APP_ENV") or "").strip().lower() != "development":
        raise DomainError("TRANSFER_REQUIRES_DEVELOPMENT")
    if (values.get("PHOTO_BACKEND") or "").strip().lower() != "local":
        raise DomainError("TRANSFER_REQUIRES_LOCAL_PHOTOS")
    photo_root = Path(values.get("PRIVATE_PHOTO_ROOT") or "var/private/photos").expanduser()
    if not photo_root.is_absolute():
        photo_root = PROJECT_ROOT / photo_root
    runtime_values = {**values, "PRIVATE_PHOTO_ROOT": str(photo_root)}
    return path, Settings.from_env(values), RuntimeSettings.from_env(runtime_values)


def _transfer_command(args):
    if args.command == "backup-local" and not args.allow_database_access:
        raise DomainError("EXPLICIT_DATABASE_ACCESS_APPROVAL_REQUIRED")
    if args.command == "restore-aiven" and not args.allow_database_changes:
        raise DomainError("EXPLICIT_DATABASE_CHANGE_APPROVAL_REQUIRED")
    if not args.writers_paused:
        raise DomainError("TRANSFER_REQUIRES_PAUSED_WRITERS")
    env_file, settings, runtime = _transfer_environment(args.env_file)
    directory = args.directory.expanduser().resolve()
    if args.command == "backup-local":
        from .backup import interactive_backup

        destination = interactive_backup(
            settings, project_root=PROJECT_ROOT, env_file=env_file, photo_root=runtime.photo_root,
            migration_directory=directory, allow_database_access=args.allow_database_access,
            writers_paused=args.writers_paused,
        )
        print("Backup completed and file checksums verified: " + str(destination))
        print("Keep the complete backup private. Source data and application configuration were not changed.")
    else:
        from .transfer import restore_aiven_backup

        result = restore_aiven_backup(
            settings, runtime, args.backup.expanduser().absolute(), directory,
            expected_target=args.expected_target, allow_database_changes=args.allow_database_changes,
            writers_paused=args.writers_paused,
        )
        print(json.dumps(result, default=str))
        print("Restore verification completed. No application startup or configuration switch was performed.")
    return 0


def _runtime_account_command(args):
    if not args.allow_database_changes:
        raise DomainError("EXPLICIT_DATABASE_CHANGE_APPROVAL_REQUIRED")
    if args.env_file is None:
        raise DomainError("EXPLICIT_ENV_FILE_REQUIRED")
    if not sys.stdin.isatty():
        raise DomainError("RUNTIME_ACCOUNT_REQUIRES_INTERACTIVE_TERMINAL")
    from .runtime_account import INSERT_TABLES, UPDATE_TABLES, create_aiven_runtime_account, target_confirmation

    _, settings, runtime = _transfer_environment(args.env_file)
    expected = target_confirmation(settings, runtime)
    output = PROJECT_ROOT / "var" / "private" / "vercel" / "database-runtime.env"
    print("Create and verify meal_runtime@% with a generated private password and mandatory SSL.")
    print("The % host scope permits authentication from any host that can reach this Aiven service.")
    print("Grant SELECT on " + settings.db_name + ".*.")
    print("Grant INSERT on these " + settings.db_name + " tables: " + ", ".join(INSERT_TABLES) + ".")
    print("Grant UPDATE on these " + settings.db_name + " tables: " + ", ".join(UPDATE_TABLES) + ".")
    print("No DELETE, schema changes, trigger creation, account management, or GRANT OPTION privileges are granted.")
    confirmation = input("Type " + expected + " to continue: ")
    result = create_aiven_runtime_account(
        settings, runtime, PROJECT_ROOT / "database" / "migrations", output,
        confirmation=confirmation, allow_database_changes=True,
    )
    if (
        not isinstance(result, dict) or result.get("status") != "verified"
        or result.get("account") != "meal_runtime@%"
        or not isinstance(result.get("credentials_file"), (str, Path))
        or Path(result["credentials_file"]).absolute() != output.absolute()
    ):
        raise DomainError("RUNTIME_ACCOUNT_VERIFICATION_UNCONFIRMED")
    print("Aiven account meal_runtime@% created and verified.")
    print("Private credentials file: " + str(output))
    print("No deployment or application configuration switch was performed.")
    return 0


def _runtime_account_failure_message(error):
    stages = {"PREFLIGHT", "PRIVATE_CREDENTIALS", "CREATE_ACCOUNT", "GRANTS", "VERIFY_ACCOUNT", "PUBLISH_CREDENTIALS"}
    stage = getattr(error, "runtime_account_stage", None)
    if not isinstance(stage, str) or stage not in stages:
        return None
    message = "Account creation stage: " + stage + "."
    number = getattr(error, "runtime_account_mysql_error", None)
    if type(number) is int and 1 <= number <= 65535:
        message += " MySQL error " + str(number) + "."
    if stage != "PREFLIGHT":
        message += " Keep any database-runtime.pending.env and database-runtime.env files private. Do not rerun the command or delete these files; share only the error code, stage, and MySQL error number for review."
    return message


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
        if args.command in {"backup-local", "restore-aiven"}:
            return _transfer_command(args)
        if args.command == "create-aiven-runtime":
            return _runtime_account_command(args)
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
        if args.command == "backup-local":
            from .backup import backup_failure_message

            message = backup_failure_message(error)
            if message:
                print(message, file=sys.stderr)
        if args.command == "create-aiven-runtime":
            message = _runtime_account_failure_message(error)
            if message:
                print(message, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Operation cancelled.", file=sys.stderr)
        return 130
    except Exception as error:
        print(_safe_failure_message(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
