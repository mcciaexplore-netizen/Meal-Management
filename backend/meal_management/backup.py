import base64
import getpass
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import warnings
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from itertools import chain
from pathlib import Path, PurePosixPath

from .config import Settings
from .database import Database, Session
from .errors import DomainError
from .migrations import load_migrations, schema_fingerprint, validate_ledger


_BACKUP_STAGES = {
    "SOURCE_SNAPSHOT", "SNAPSHOT_ENCODING", "PRIVATE_FILE_CHECK", "TEMPORARY_CREDENTIALS", "ORIGINAL_EXPORT",
    "PORTABLE_EXPORT", "TRIGGER_METADATA", "CONFIGURATION_COPY", "PHOTO_COPY",
    "SOURCE_RECHECK", "FILE_CHECKSUMS", "COMPLETION",
}


def _cleanup_preserving_error(action):
    failing = sys.exc_info()[0] is not None
    try:
        action()
    except Exception:
        if not failing:
            raise


def _backup_failure(error, stage):
    from mysql.connector import Error as MySQLError

    diagnostic = {"stage": stage, "category": "UNEXPECTED_ERROR", "mysql_error_number": None}
    if isinstance(error, MySQLError):
        diagnostic["category"] = "MYSQL_ERROR"
        number = getattr(error, "errno", None)
        if type(number) is int and 1 <= number <= 65535:
            diagnostic["mysql_error_number"] = number
    elif isinstance(error, subprocess.TimeoutExpired):
        diagnostic["category"] = "EXPORT_TIMEOUT"
    elif isinstance(error, PermissionError):
        diagnostic["category"] = "FILE_PERMISSION_ERROR"
    elif isinstance(error, OSError):
        diagnostic["category"] = "FILE_SYSTEM_ERROR"
    elif isinstance(error, (TypeError, ValueError, KeyError, AttributeError, IndexError)):
        diagnostic["category"] = "DATA_PROCESSING_ERROR"
    failure = DomainError("BACKUP_FAILED_PARTIAL_DIRECTORY_PRESERVED")
    failure.backup_diagnostic = diagnostic
    return failure


def backup_failure_message(error):
    diagnostic = getattr(error, "backup_diagnostic", None)
    if not isinstance(diagnostic, dict):
        return None
    stage = diagnostic.get("stage")
    if not isinstance(stage, str) or stage not in _BACKUP_STAGES:
        return None
    descriptions = {
        "MYSQL_ERROR": "The database operation failed.",
        "EXPORT_TIMEOUT": "The export exceeded its time limit.",
        "FILE_PERMISSION_ERROR": "The backup could not access a required local file or directory.",
        "FILE_SYSTEM_ERROR": "A local file operation failed.",
        "DATA_PROCESSING_ERROR": "The backup could not process a database value or metadata result.",
        "UNEXPECTED_ERROR": "An unexpected backup operation failed.",
    }
    category = diagnostic.get("category")
    if not isinstance(category, str) or category not in descriptions:
        return None
    message = "Backup stage: " + stage + ". "
    number = diagnostic.get("mysql_error_number")
    if category == "MYSQL_ERROR" and type(number) is int and 1 <= number <= 65535:
        message += "MySQL error " + str(number) + ". "
        if number == 1045:
            return message + "MySQL rejected the login. Check the local MySQL root password and account access."
        if number == 1044:
            return message + "This account cannot access the selected database."
        if number == 1049:
            return message + "The selected database does not exist."
        if number in {2002, 2003, 2005}:
            return message + "Could not connect to MySQL. Check that the local server is running and its connection settings match."
    return message + descriptions[category]


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,64}", value):
        raise DomainError("BACKUP_UNSUPPORTED_IDENTIFIER")
    return "`" + value + "`"


def canonical_value(value):
    if value is None:
        return ["null"]
    if isinstance(value, bool):
        return ["bool", value]
    if isinstance(value, int):
        return ["int", str(value)]
    if isinstance(value, Decimal):
        return ["decimal", str(value)]
    if isinstance(value, float) and math.isfinite(value):
        return ["float", value.hex()]
    if isinstance(value, str):
        return ["str", value]
    if isinstance(value, (bytes, bytearray, memoryview)):
        return ["bytes", base64.b64encode(value).decode("ascii")]
    if isinstance(value, datetime):
        return ["datetime", value.isoformat(timespec="microseconds")]
    if isinstance(value, date):
        return ["date", value.isoformat()]
    if isinstance(value, time):
        return ["time", value.isoformat(timespec="microseconds")]
    if isinstance(value, timedelta):
        return ["timedelta", str((value.days * 86400 + value.seconds) * 1000000 + value.microseconds)]
    if isinstance(value, set) and all(isinstance(item, str) for item in value):
        return ["set", sorted(value)]
    raise DomainError("BACKUP_UNSUPPORTED_COLUMN_VALUE")


def _json_bytes(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")) + "\n").encode("utf-8")


def normalize_sql_mode(value):
    if isinstance(value, str):
        modes = value.split(",") if value else []
    elif isinstance(value, (set, frozenset)):
        modes = list(value)
    else:
        raise DomainError("BACKUP_INVALID_TRIGGER_SQL_MODE")
    if any(not isinstance(mode, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", mode) for mode in modes):
        raise DomainError("BACKUP_INVALID_TRIGGER_SQL_MODE")
    return ",".join(sorted(set(modes)))


def _expected_trigger_names(migrations, ledger):
    applied = {row["version"] for row in ledger}
    names = set()
    for migration in migrations:
        if migration.version not in applied:
            continue
        for statement in migration.statements:
            created = re.match(r"CREATE\s+TRIGGER\s+`?([A-Za-z0-9_]+)`?\s", statement, re.IGNORECASE)
            removed = re.match(r"DROP\s+TRIGGER\s+(?:IF\s+EXISTS\s+)?`?([A-Za-z0-9_]+)`?(?:\s|$)", statement, re.IGNORECASE)
            if created:
                names.add(created[1])
            if removed:
                names.discard(removed[1])
    return names


def table_digest(connection, table_name, *, columns=None):
    table = _identifier(table_name)
    tx = Session(connection)
    available = [row["name"] for row in tx.all(
        "SELECT COLUMN_NAME AS name FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s ORDER BY ORDINAL_POSITION",
        (table_name,),
    )]
    selected = available if columns is None else list(columns)
    if not selected or len(set(selected)) != len(selected) or any(name not in available for name in selected):
        raise DomainError("BACKUP_INVALID_COLUMNS")
    primary = [row["name"] for row in tx.all(
        "SELECT COLUMN_NAME AS name FROM information_schema.STATISTICS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND INDEX_NAME = 'PRIMARY' "
        "ORDER BY SEQ_IN_INDEX", (table_name,),
    )]
    if not primary:
        raise DomainError("BACKUP_REQUIRES_PRIMARY_KEYS")
    cursor = connection.cursor(buffered=False)
    digest = hashlib.sha256(_json_bytes(selected))
    count = 0
    try:
        cursor.execute(
            "SELECT " + ", ".join(_identifier(name) for name in selected) + " FROM " + table
            + " ORDER BY " + ", ".join(_identifier(name) for name in primary)
        )
        while True:
            rows = cursor.fetchmany(256)
            if not rows:
                break
            for row in rows:
                digest.update(_json_bytes([canonical_value(value) for value in row]))
                count += 1
    finally:
        _cleanup_preserving_error(cursor.close)
    return count, digest.hexdigest()


def snapshot_database(database, migration_directory):
    migrations = load_migrations(migration_directory)
    connection = database._connect()
    try:
        connection.start_transaction(isolation_level="REPEATABLE READ", consistent_snapshot=True, readonly=True)
        tx = Session(connection)
        version = tx.one("SELECT VERSION() AS version")["version"]
        if not re.match(r"^8\.4(?:\.|$)", version):
            raise DomainError("BACKUP_REQUIRES_MYSQL_8_4")
        name = tx.one("SELECT DATABASE() AS name")["name"]
        _identifier(name)
        objects = tx.all(
            "SELECT TABLE_NAME AS name, TABLE_TYPE AS type, ENGINE AS engine "
            "FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() ORDER BY TABLE_NAME"
        )
        if not objects or any(row["type"] != "BASE TABLE" or row["engine"] != "InnoDB" for row in objects):
            raise DomainError("BACKUP_REQUIRES_INNODB_BASE_TABLES")
        if tx.all("SELECT ROUTINE_NAME FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA = DATABASE()"):
            raise DomainError("BACKUP_UNSUPPORTED_ROUTINES")
        if tx.all("SELECT EVENT_NAME FROM information_schema.EVENTS WHERE EVENT_SCHEMA = DATABASE()"):
            raise DomainError("BACKUP_UNSUPPORTED_EVENTS")
        if "schema_migrations" not in {row["name"] for row in objects}:
            raise DomainError("BACKUP_REQUIRES_MIGRATION_LEDGER")
        ledger = tx.all("SELECT version, name, checksum, status FROM schema_migrations ORDER BY version")
        validate_ledger(migrations, ledger)
        triggers = tx.all(
            "SELECT TRIGGER_NAME AS name, EVENT_OBJECT_TABLE AS table_name, ACTION_TIMING AS timing, "
            "EVENT_MANIPULATION AS event, ACTION_STATEMENT AS body, ACTION_ORDER AS action_order, "
            "CAST(SQL_MODE AS CHAR) AS sql_mode, CHARACTER_SET_CLIENT AS character_set_client, "
            "COLLATION_CONNECTION AS collation_connection, DATABASE_COLLATION AS database_collation, "
            "DEFINER AS definer FROM information_schema.TRIGGERS "
            "WHERE TRIGGER_SCHEMA = DATABASE() ORDER BY EVENT_OBJECT_TABLE, ACTION_TIMING, "
            "EVENT_MANIPULATION, ACTION_ORDER, TRIGGER_NAME"
        )
        triggers = [{**trigger, "sql_mode": normalize_sql_mode(trigger["sql_mode"])} for trigger in triggers]
        if not _expected_trigger_names(migrations, ledger) <= {row["name"] for row in triggers}:
            raise DomainError("BACKUP_EXPECTED_TRIGGERS_MISSING_OR_NOT_VISIBLE")
        columns = {}
        counts = {}
        digests = {}
        for row in objects:
            table_name = row["name"]
            _identifier(table_name)
            columns[table_name] = [item["name"] for item in tx.all(
                "SELECT COLUMN_NAME AS name FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s ORDER BY ORDINAL_POSITION", (table_name,),
            )]
            counts[table_name], digests[table_name] = table_digest(connection, table_name, columns=columns[table_name])
        photo_keys = []
        if "selfie_object_key" in columns.get("employees", ()):
            photo_keys = sorted({row["key"] for row in tx.all(
                "SELECT selfie_object_key AS `key` FROM employees WHERE selfie_object_key IS NOT NULL"
            )})
        return {
            "version": version,
            "database": name,
            "schema_fingerprint": schema_fingerprint(tx),
            "migrations": ledger,
            "table_columns": columns,
            "table_counts": counts,
            "table_sha256": digests,
            "triggers": triggers,
            "photo_keys": photo_keys,
        }
    finally:
        try:
            _cleanup_preserving_error(connection.rollback)
        finally:
            _cleanup_preserving_error(connection.close)


def _safe_path(path):
    path = Path(os.path.abspath(path))
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise DomainError("BACKUP_UNSAFE_PATH")
    return path


@contextmanager
def _read_file(path):
    path = _safe_path(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        attributes = os.fstat(source.fileno())
        if not stat.S_ISREG(attributes.st_mode) or attributes.st_nlink != 1:
            raise DomainError("BACKUP_UNSAFE_FILE")
        yield source


@contextmanager
def _new_file(path):
    descriptor = os.open(_safe_path(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        yield output
        output.flush()
        os.fsync(output.fileno())


def _file_digest(path):
    digest = hashlib.sha256()
    with _read_file(path) as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _copy_file(source, destination):
    with _read_file(source) as original, _new_file(destination) as output:
        shutil.copyfileobj(original, output, length=1024 * 1024)


def _photo_files(root):
    root = _safe_path(root)
    if not root.exists():
        return {}
    if not root.is_dir():
        raise DomainError("BACKUP_UNSAFE_PHOTO_ROOT")
    result = {}
    for path in root.iterdir():
        if len(result) >= 100000 or not path.is_file() or path.is_symlink():
            raise DomainError("BACKUP_UNSUPPORTED_PHOTO_STORAGE")
        result[path.name] = _file_digest(path)
    return result


def _mkdir_private(path):
    path = _safe_path(path)
    if not path.exists():
        _mkdir_private(path.parent)
        path.mkdir(mode=0o700)
    if not path.is_dir():
        raise DomainError("BACKUP_UNSAFE_PATH")


def _defaults_value(value):
    value = str(value)
    if "\x00" in value:
        raise DomainError("BACKUP_INVALID_CREDENTIAL")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t") + '"'


@contextmanager
def _client_defaults(settings, directory):
    path = directory / (".mysql-" + secrets.token_hex(12) + ".cnf")
    created = False
    options = {
        "host": settings.db_host,
        "port": settings.db_port,
        "user": settings.db_user,
        "password": settings.db_password,
        "protocol": "TCP",
        "default-character-set": "utf8mb4",
    }
    if settings.db_ssl_ca:
        options.update({"ssl-ca": settings.db_ssl_ca, "ssl-mode": "VERIFY_IDENTITY"})
    try:
        with _new_file(path) as output:
            created = True
            output.write(("[client]\n" + "".join(key + "=" + _defaults_value(value) + "\n" for key, value in options.items())).encode("utf-8"))
        yield path
    finally:
        if created:
            path.unlink(missing_ok=True)


def _dump_arguments(binary, defaults, database_name, *, triggers):
    return [
        binary, "--defaults-file=" + str(defaults), "--no-login-paths",
        "--single-transaction", "--skip-lock-tables", "--no-tablespaces", "--set-gtid-purged=OFF",
        "--skip-add-drop-table", "--skip-add-drop-trigger", "--skip-add-locks", "--skip-disable-keys",
        "--hex-blob", "--column-statistics=0", "--order-by-primary", "--tz-utc",
        "--default-character-set=utf8mb4", "--skip-routines", "--skip-events",
        "--triggers" if triggers else "--skip-triggers", database_name,
    ]


def _dump(binary, defaults, database_name, destination, *, triggers):
    arguments = _dump_arguments(binary, defaults, database_name, triggers=triggers)
    with _new_file(destination) as output:
        result = subprocess.run(
            arguments, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL,
            env={"PATH": os.environ.get("PATH", ""), "LC_ALL": "C"}, check=False, timeout=3600,
        )
    if result.returncode or destination.stat().st_size == 0:
        raise DomainError("BACKUP_MYSQLDUMP_FAILED")


def _verify_environment(path, settings):
    from dotenv import dotenv_values

    with _read_file(path) as source:
        import io

        content = source.read(1024 * 1024 + 1)
        if len(content) > 1024 * 1024:
            raise DomainError("BACKUP_ENVIRONMENT_TOO_LARGE")
        saved = Settings.from_env(dotenv_values(stream=io.StringIO(content.decode("utf-8")), interpolate=False))
    if saved != settings:
        raise DomainError("BACKUP_ENVIRONMENT_DIFFERS_FROM_ACTIVE_CONFIGURATION")


def create_local_backup(settings, *, project_root, env_file, photo_root, migration_directory, root_password, mysqldump="mysqldump"):
    if settings.db_host.lower() not in {"localhost", "127.0.0.1", "::1"}:
        raise DomainError("BACKUP_REQUIRES_LOCAL_DATABASE")
    _identifier(settings.db_name)
    if not isinstance(root_password, str) or not root_password:
        raise DomainError("BACKUP_ROOT_PASSWORD_REQUIRED")
    root = _safe_path(Path(project_root).resolve(strict=True))
    environment = _safe_path(env_file)
    photos = _safe_path(photo_root)
    output_root = _safe_path(root / "var" / "private" / "backups")
    if output_root == photos or photos in output_root.parents or output_root in photos.parents:
        raise DomainError("BACKUP_PHOTO_ROOT_OVERLAPS_BACKUPS")
    if environment == photos or photos in environment.parents:
        raise DomainError("BACKUP_PHOTO_ROOT_INCLUDES_CONFIGURATION")
    _verify_environment(environment, settings)
    binary = shutil.which(str(mysqldump))
    if binary is None:
        raise DomainError("BACKUP_MYSQLDUMP_NOT_FOUND")
    version = subprocess.run(
        [binary, "--no-defaults", "--no-login-paths", "--version"], capture_output=True,
        env={"PATH": os.environ.get("PATH", ""), "LC_ALL": "C"}, check=False, timeout=10,
    )
    if version.returncode or not re.search(rb"\b(?:Ver|Distrib)\s+8\.4(?:\.|\s)", version.stdout):
        raise DomainError("BACKUP_REQUIRES_MYSQLDUMP_8_4")
    _mkdir_private(output_root)
    if output_root.stat().st_uid != os.getuid() or stat.S_IMODE(output_root.stat().st_mode) != 0o700:
        raise DomainError("BACKUP_DIRECTORY_REQUIRES_PRIVATE_PERMISSIONS")
    created_at = datetime.now(timezone.utc)
    destination = output_root / (created_at.strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(8))
    destination.mkdir(mode=0o700)
    root_settings = replace(settings, db_user="root", db_password=root_password)
    stage = "SOURCE_SNAPSHOT"
    try:
        before = snapshot_database(Database(root_settings), migration_directory)
        stage = "SNAPSHOT_ENCODING"
        trigger_bytes = _json_bytes(before["triggers"])
        _json_bytes(before)
        stage = "PRIVATE_FILE_CHECK"
        photo_hashes = _photo_files(photos)
        if any(key not in photo_hashes for key in before["photo_keys"]):
            raise DomainError("BACKUP_REFERENCED_PHOTO_MISSING")
        environment_hash = _file_digest(environment)
        stage = "TEMPORARY_CREDENTIALS"
        with _client_defaults(root_settings, destination) as defaults:
            stage = "ORIGINAL_EXPORT"
            _dump(binary, defaults, settings.db_name, destination / "original.sql", triggers=True)
            stage = "PORTABLE_EXPORT"
            _dump(binary, defaults, settings.db_name, destination / "portable.sql", triggers=False)
        stage = "TRIGGER_METADATA"
        with _new_file(destination / "triggers.json") as output:
            output.write(trigger_bytes)
        stage = "CONFIGURATION_COPY"
        _copy_file(environment, destination / "environment.env")
        stage = "PHOTO_COPY"
        (destination / "photos").mkdir(mode=0o700)
        for filename in photo_hashes:
            _copy_file(photos / filename, destination / "photos" / filename)
        stage = "SOURCE_RECHECK"
        after = snapshot_database(Database(root_settings), migration_directory)
        if before != after or photo_hashes != _photo_files(photos) or environment_hash != _file_digest(environment):
            raise DomainError("BACKUP_SOURCE_CHANGED_KEEP_WRITERS_PAUSED")
        stage = "FILE_CHECKSUMS"
        files = {str(path.relative_to(destination).as_posix()): _file_digest(path) for path in sorted(destination.rglob("*")) if path.is_file()}
        if files["environment.env"] != environment_hash or any(files["photos/" + name] != checksum for name, checksum in photo_hashes.items()):
            raise DomainError("BACKUP_PRIVATE_FILES_CHANGED_DURING_COPY")
        manifest = {
            "format_version": 1, "snapshot": before, "files": files,
            "photo_root": str(photos), "created_at": created_at.isoformat(),
        }
        stage = "COMPLETION"
        manifest_bytes = _json_bytes(manifest)
        with _new_file(destination / "manifest.json") as output:
            output.write(manifest_bytes)
        with _new_file(destination / "COMPLETE") as output:
            output.write((hashlib.sha256(manifest_bytes).hexdigest() + "\n").encode("ascii"))
        load_verified_backup(destination)
        return destination
    except DomainError:
        (destination / "COMPLETE").unlink(missing_ok=True)
        raise
    except Exception as error:
        (destination / "COMPLETE").unlink(missing_ok=True)
        failure = _backup_failure(error, stage)
        try:
            with _new_file(destination / "failure.json") as output:
                output.write(_json_bytes(failure.backup_diagnostic))
        except Exception:
            pass
        raise failure from None
    except BaseException:
        (destination / "COMPLETE").unlink(missing_ok=True)
        raise


def load_verified_backup(path):
    root = _safe_path(path)
    try:
        if not root.is_dir():
            raise DomainError("BACKUP_DIRECTORY_NOT_FOUND")
        actual = set()
        for item in chain((root,), root.rglob("*")):
            if item.is_symlink():
                raise DomainError("BACKUP_UNSAFE_PATH")
            attributes = item.stat()
            expected = 0o700 if item.is_dir() else 0o600
            if attributes.st_uid != os.getuid() or stat.S_IMODE(attributes.st_mode) != expected:
                raise DomainError("BACKUP_REQUIRES_PRIVATE_PERMISSIONS")
            if item.is_file():
                if attributes.st_nlink != 1:
                    raise DomainError("BACKUP_UNSAFE_FILE")
                actual.add(item.relative_to(root).as_posix())
            elif not item.is_dir():
                raise DomainError("BACKUP_UNSAFE_FILE")
            elif item != root and item.relative_to(root).as_posix() != "photos":
                raise DomainError("BACKUP_UNEXPECTED_DIRECTORY")
            if len(actual) > 100010:
                raise DomainError("BACKUP_TOO_MANY_FILES")
        with _read_file(root / "manifest.json") as source:
            data = source.read(16 * 1024 * 1024 + 1)
        if len(data) > 16 * 1024 * 1024:
            raise DomainError("BACKUP_MANIFEST_TOO_LARGE")
        with _read_file(root / "COMPLETE") as source:
            marker = source.read(66)
        if marker != (hashlib.sha256(data).hexdigest() + "\n").encode("ascii"):
            raise DomainError("BACKUP_COMPLETION_CHECKSUM_MISMATCH")
        manifest = json.loads(data)
        if manifest.get("format_version") != 1 or not isinstance(manifest.get("snapshot"), dict) or not isinstance(manifest.get("files"), dict):
            raise DomainError("BACKUP_UNSUPPORTED_FORMAT")
        files = manifest["files"]
        required = {"original.sql", "portable.sql", "triggers.json", "environment.env"}
        if not required <= set(files) or set(files) | {"manifest.json", "COMPLETE"} != actual:
            raise DomainError("BACKUP_FILE_MANIFEST_MISMATCH")
        for name, checksum in files.items():
            relative = PurePosixPath(name)
            if relative.is_absolute() or relative.as_posix() != name or ".." in relative.parts or "\\" in name:
                raise DomainError("BACKUP_INVALID_MANIFEST_PATH")
            if name not in required and (len(relative.parts) != 2 or relative.parts[0] != "photos"):
                raise DomainError("BACKUP_UNEXPECTED_FILE")
            if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum) or _file_digest(root / name) != checksum:
                raise DomainError("BACKUP_FILE_CHECKSUM_MISMATCH")
        with _read_file(root / "triggers.json") as source:
            triggers = json.load(source)
        if triggers != manifest["snapshot"].get("triggers"):
            raise DomainError("BACKUP_TRIGGER_METADATA_MISMATCH")
        if any((root / name).stat().st_size == 0 for name in required):
            raise DomainError("BACKUP_EMPTY_REQUIRED_FILE")
        return manifest
    except DomainError:
        raise
    except Exception:
        raise DomainError("BACKUP_INCOMPLETE_OR_UNREADABLE") from None


def interactive_backup(settings, *, project_root, env_file, photo_root, migration_directory, allow_database_access=False, writers_paused=False, mysqldump="mysqldump"):
    if not allow_database_access:
        raise DomainError("BACKUP_REQUIRES_ALLOW_DATABASE_ACCESS")
    if not writers_paused:
        raise DomainError("BACKUP_REQUIRES_WRITERS_PAUSED")
    if settings.db_host.lower() not in {"localhost", "127.0.0.1", "::1"}:
        raise DomainError("BACKUP_REQUIRES_LOCAL_DATABASE")
    if not sys.stdin.isatty():
        raise DomainError("BACKUP_REQUIRES_INTERACTIVE_TERMINAL")
    confirmation = "BACKUP " + settings.db_host + ":" + str(settings.db_port) + "/" + settings.db_name
    if input("Keep all local writers paused. Type " + confirmation + " to continue: ") != confirmation:
        raise DomainError("BACKUP_CONFIRMATION_MISMATCH")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = getpass.getpass("Local MySQL root password (used only for this backup): ")
    except getpass.GetPassWarning:
        raise DomainError("SECURE_PASSWORD_PROMPT_UNAVAILABLE") from None
    return create_local_backup(
        settings, project_root=project_root, env_file=env_file, photo_root=photo_root,
        migration_directory=migration_directory, root_password=password, mysqldump=mysqldump,
    )
